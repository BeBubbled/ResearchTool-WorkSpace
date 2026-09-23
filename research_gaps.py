"""Local research-gap projects, paper ingestion, and evidence analysis."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import threading
import time
from typing import Any, Callable
from urllib.parse import quote, urljoin, urlsplit
import uuid

import requests
from bs4 import BeautifulSoup
from flask import Blueprint, Response, jsonify, request

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None  # type: ignore[assignment,misc]


SEMANTIC_SCHOLAR_BASE_URL = "https://api.semanticscholar.org/graph/v1"
SEMANTIC_SCHOLAR_FIELDS = (
    "paperId,title,abstract,year,venue,authors,url,externalIds,openAccessPdf,"
    "publicationDate,citationCount,referenceCount"
)
MAX_PDF_BYTES = 50 * 1024 * 1024
MAX_ANALYSIS_CHARS = 96_000
ANALYSIS_CHUNK_CHARS = 10_000
VALID_RELATIONS = {"proposes", "addresses", "partially_solves", "solves", "fails_on"}
VALID_REVIEWS = {"pending", "accepted", "rejected"}
VALID_FACT_KINDS = {
    "problem", "task", "method", "dataset", "metric", "contribution",
    "limitation", "failure_condition", "gap",
}


def now() -> float:
    return time.time()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def normalized_title(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", value.casefold()).strip()


def clean_text(value: Any, limit: int = 20_000) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def clean_multiline(value: Any, limit: int = 120_000) -> str:
    return str(value or "").replace("\x00", "").strip()[:limit]


def parse_external_identifier(value: str) -> tuple[str, str] | None:
    """Return a Semantic Scholar identifier prefix and normalized value."""
    raw = value.strip()
    if not raw:
        return None
    parsed = urlsplit(raw if "://" in raw else f"https://placeholder.invalid/{raw}")
    host = parsed.hostname.casefold() if parsed.hostname else ""
    path = parsed.path.strip("/")
    if host in {"doi.org", "dx.doi.org"} and path:
        return "DOI", path
    if host.endswith("arxiv.org"):
        match = re.search(r"(?:abs|pdf)/([^/?#]+)", parsed.path, re.I)
        if match:
            return "ARXIV", re.sub(r"\.pdf$", "", match.group(1), flags=re.I)
    if "semanticscholar.org" in host:
        match = re.search(r"/paper/(?:[^/]+/)?([0-9a-f]{40})", parsed.path, re.I)
        if match:
            return "S2", match.group(1).lower()
    doi = re.search(r"(?:doi:\s*)?(10\.\d{4,9}/[-._;()/:a-z0-9]+)$", raw, re.I)
    if doi:
        return "DOI", doi.group(1).rstrip(".,)")
    arxiv = re.fullmatch(r"(?:arxiv:\s*)?((?:\d{4}\.\d{4,5}|[a-z-]+/\d{7})(?:v\d+)?)", raw, re.I)
    if arxiv:
        return "ARXIV", arxiv.group(1)
    if re.fullmatch(r"[0-9a-f]{40}", raw, re.I):
        return "S2", raw.lower()
    return None


def extract_json_object(value: str) -> dict[str, Any]:
    text = value.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("LLM did not return a JSON object.")
        payload = json.loads(text[start:end + 1])
    if not isinstance(payload, dict):
        raise ValueError("LLM response must be a JSON object.")
    return payload


def text_contains_evidence(source: str, evidence: str) -> bool:
    needle = clean_text(evidence, 2_000).casefold()
    return bool(needle) and needle in clean_text(source, len(source) + 1).casefold()


def split_text(text: str, chunk_chars: int = ANALYSIS_CHUNK_CHARS) -> list[str]:
    text = clean_multiline(text, MAX_ANALYSIS_CHARS)
    if not text:
        return []
    paragraphs = re.split(r"\n\s*\n", text)
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for paragraph in paragraphs:
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if current and length + len(paragraph) + 2 > chunk_chars:
            chunks.append("\n\n".join(current))
            current, length = [], 0
        while len(paragraph) > chunk_chars:
            if current:
                chunks.append("\n\n".join(current))
                current, length = [], 0
            chunks.append(paragraph[:chunk_chars])
            paragraph = paragraph[chunk_chars:]
        current.append(paragraph)
        length += len(paragraph) + 2
    if current:
        chunks.append("\n\n".join(current))
    max_chunks = max(1, (MAX_ANALYSIS_CHARS + chunk_chars - 1) // chunk_chars)
    return chunks[:max_chunks]


def extract_pdf_text(path: Path) -> tuple[str, int]:
    if PdfReader is None:
        raise RuntimeError("pypdf is required to extract PDF text.")
    reader = PdfReader(str(path), strict=False)
    pages: list[str] = []
    for index, page in enumerate(reader.pages):
        content = clean_multiline(page.extract_text() or "", 100_000)
        if content:
            pages.append(f"[Page {index + 1}]\n{content}")
        if sum(len(item) for item in pages) >= MAX_ANALYSIS_CHARS:
            break
    return "\n\n".join(pages)[:MAX_ANALYSIS_CHARS], len(reader.pages)


def extract_local_text(path: Path, render_kind: str) -> tuple[str, str]:
    if render_kind == "pdf" or path.suffix.lower() == ".pdf":
        text, _ = extract_pdf_text(path)
        return text, "page"
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    if render_kind == "html" or path.suffix.lower() in {".html", ".htm"}:
        soup = BeautifulSoup(raw, "html.parser")
        for node in soup(["script", "style", "noscript"]):
            node.decompose()
        blocks: list[str] = []
        for index, node in enumerate(soup.find_all(["h1", "h2", "h3", "p", "li", "figcaption"])):
            value = clean_text(node.get_text(" ", strip=True), 20_000)
            if value:
                locator = node.get("data-reader-pair-id") or f"html-{index + 1}"
                blocks.append(f"[Block {locator}]\n{value}")
        return "\n\n".join(blocks)[:MAX_ANALYSIS_CHARS], "block"
    return raw[:MAX_ANALYSIS_CHARS], "section"


def safe_download_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Open-access PDF URL must use HTTPS.")
    hostname = parsed.hostname.casefold()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".local"):
        raise ValueError("Open-access PDF URL points to a local host.")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, parsed.port or 443)}
    except socket.gaierror as exc:
        raise ValueError("Open-access PDF host could not be resolved.") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError("Open-access PDF URL resolves to a non-public address.")
    return value


def download_pdf(url: str, destination: Path, session: requests.Session | None = None) -> Path:
    client = session or requests.Session()
    current = safe_download_url(url)
    for _ in range(5):
        response = client.get(current, stream=True, timeout=(15, 45), allow_redirects=False)
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("Location")
            if not location:
                raise ValueError("PDF redirect did not include a destination.")
            current = safe_download_url(urljoin(current, location))
            continue
        response.raise_for_status()
        declared = int(response.headers.get("Content-Length") or 0)
        if declared > MAX_PDF_BYTES:
            raise ValueError("Open-access PDF is larger than 50 MB.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".download")
        total = 0
        prefix = b""
        try:
            with temporary.open("wb") as handle:
                for chunk in response.iter_content(64 * 1024):
                    if not chunk:
                        continue
                    if not prefix:
                        prefix = chunk[:5]
                    total += len(chunk)
                    if total > MAX_PDF_BYTES:
                        raise ValueError("Open-access PDF is larger than 50 MB.")
                    handle.write(chunk)
            if prefix != b"%PDF-":
                raise ValueError("Open-access URL did not return a PDF file.")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination
    raise ValueError("Open-access PDF redirected too many times.")


class ResearchGapStore:
    def __init__(self, database: Path) -> None:
        self.database = database
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._migrate_lock = threading.Lock()
        self.migrate()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def migrate(self) -> None:
        with self._migrate_lock, self.connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, topic TEXT NOT NULL,
                    query TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS papers (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    source_kind TEXT NOT NULL, source_key TEXT NOT NULL, semantic_scholar_id TEXT,
                    doi TEXT, arxiv_id TEXT, reader_cache_key TEXT, title TEXT NOT NULL,
                    abstract TEXT NOT NULL DEFAULT '', authors_json TEXT NOT NULL DEFAULT '[]',
                    year INTEGER, venue TEXT NOT NULL DEFAULT '', external_url TEXT NOT NULL DEFAULT '',
                    pdf_url TEXT NOT NULL DEFAULT '', citation_count INTEGER NOT NULL DEFAULT 0,
                    reference_count INTEGER NOT NULL DEFAULT 0, evidence_level TEXT NOT NULL DEFAULT 'abstract',
                    content_path TEXT, locator_mode TEXT NOT NULL DEFAULT 'abstract', status TEXT NOT NULL DEFAULT 'ready',
                    error TEXT, metadata_json TEXT NOT NULL DEFAULT '{}', analyzed_at REAL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    UNIQUE(project_id, source_key)
                );
                CREATE TABLE IF NOT EXISTS facts (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL, title TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0.5, evidence TEXT NOT NULL DEFAULT '',
                    locator TEXT NOT NULL DEFAULT '', review_status TEXT NOT NULL DEFAULT 'pending',
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS gaps (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    canonical_title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', domain TEXT NOT NULL DEFAULT '',
                    task TEXT NOT NULL DEFAULT '', failure_condition TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'OPEN', provisional INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS relations (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
                    gap_id TEXT NOT NULL REFERENCES gaps(id) ON DELETE CASCADE,
                    relation_type TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 0.5,
                    evidence TEXT NOT NULL DEFAULT '', locator TEXT NOT NULL DEFAULT '',
                    rationale TEXT NOT NULL DEFAULT '', review_status TEXT NOT NULL DEFAULT 'pending',
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    UNIQUE(paper_id, gap_id, relation_type, evidence)
                );
                CREATE TABLE IF NOT EXISTS analysis_jobs (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    paper_ids_json TEXT NOT NULL, preset_id TEXT NOT NULL, status TEXT NOT NULL,
                    stage TEXT NOT NULL, completed INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
                    current_paper_id TEXT, logs_json TEXT NOT NULL DEFAULT '[]', error TEXT,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL, finished_at REAL
                );
                CREATE INDEX IF NOT EXISTS facts_paper_idx ON facts(paper_id);
                CREATE INDEX IF NOT EXISTS relations_gap_idx ON relations(gap_id);
                CREATE INDEX IF NOT EXISTS papers_project_idx ON papers(project_id);
                """
            )
            db.execute(
                "UPDATE analysis_jobs SET status='interrupted', stage='interrupted', "
                "error=COALESCE(error, '应用重启，任务已中断，可重新提交。'), updated_at=? "
                "WHERE status IN ('queued','running')",
                (now(),),
            )

    def project(self, project_id: str) -> sqlite3.Row | None:
        with self.connect() as db:
            return db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()


@dataclass
class PaperSource:
    text: str
    evidence_level: str
    locator_mode: str
    content_path: str | None
    warning: str | None = None


class SemanticScholarClient:
    def __init__(self, root: Path, session: requests.Session | None = None) -> None:
        self.root = root
        self.session = session or requests.Session()
        self.lock = threading.Lock()
        self.last_request = 0.0

    def _request(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        cache_key = hashlib.sha256(json.dumps([path, params], sort_keys=True).encode()).hexdigest()
        cache_path = self.root / "metadata-cache" / f"{cache_key}.json"
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if now() - float(cached["cachedAt"]) < 7 * 24 * 60 * 60:
                return cached["payload"]
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            pass
        with self.lock:
            delay = 1.05 - (time.monotonic() - self.last_request)
            if delay > 0:
                time.sleep(delay)
            headers = {"User-Agent": "ResearchToolkit/1.0"}
            api_key = os.getenv("SEMANTIC_SCHOLAR_API_KEY", "").strip()
            if api_key:
                headers["x-api-key"] = api_key
            response = self.session.get(
                f"{SEMANTIC_SCHOLAR_BASE_URL}{path}", params=params, headers=headers, timeout=(10, 30)
            )
            self.last_request = time.monotonic()
        if response.status_code == 429:
            raise RuntimeError("Semantic Scholar 请求过于频繁，请稍后重试或配置 API Key。")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Semantic Scholar returned an invalid response.")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({"cachedAt": now(), "payload": payload}, ensure_ascii=False), encoding="utf-8"
        )
        return payload

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        exact = parse_external_identifier(query)
        if exact:
            prefix, value = exact
            identifier = value if prefix == "S2" else f"{prefix}:{value}"
            try:
                return [self.paper(identifier)]
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 404:
                    return []
                raise
        payload = self._request(
            "/paper/search", {"query": query, "limit": max(1, min(20, limit)), "fields": SEMANTIC_SCHOLAR_FIELDS}
        )
        return [self.normalize(item) for item in payload.get("data", []) if isinstance(item, dict)]

    def paper(self, identifier: str) -> dict[str, Any]:
        payload = self._request(f"/paper/{quote(identifier, safe=':')}", {"fields": SEMANTIC_SCHOLAR_FIELDS})
        return self.normalize(payload)

    @staticmethod
    def normalize(item: dict[str, Any]) -> dict[str, Any]:
        external = item.get("externalIds") if isinstance(item.get("externalIds"), dict) else {}
        open_pdf = item.get("openAccessPdf") if isinstance(item.get("openAccessPdf"), dict) else {}
        authors = [clean_text(author.get("name"), 200) for author in item.get("authors", []) if isinstance(author, dict)]
        return {
            "paperId": clean_text(item.get("paperId"), 100),
            "title": clean_text(item.get("title"), 1_000),
            "abstract": clean_multiline(item.get("abstract"), 100_000),
            "year": item.get("year") if isinstance(item.get("year"), int) else None,
            "venue": clean_text(item.get("venue"), 500),
            "authors": authors,
            "url": clean_text(item.get("url"), 2_000),
            "doi": clean_text(external.get("DOI"), 500),
            "arxivId": clean_text(external.get("ArXiv"), 200),
            "pdfUrl": clean_text(open_pdf.get("url"), 2_000),
            "publicationDate": clean_text(item.get("publicationDate"), 50),
            "citationCount": int(item.get("citationCount") or 0),
            "referenceCount": int(item.get("referenceCount") or 0),
        }


class ResearchGapService:
    def __init__(
        self,
        root: Path,
        llm_request: Callable[[str, list[dict[str, str]]], str],
        llm_presets: Callable[[], list[dict[str, Any]]],
        reader_source: Callable[[str], dict[str, Any] | None],
    ) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = ResearchGapStore(root / "research_gaps.sqlite3")
        self.semantic_scholar = SemanticScholarClient(root)
        self.llm_request = llm_request
        self.llm_presets = llm_presets
        self.reader_source = reader_source
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="research-gap")

    def _paper_dir(self, project_id: str, paper_id: str) -> Path:
        return self.root / "projects" / project_id / "papers" / paper_id

    def create_project(self, name: Any, topic: Any, query: Any = "") -> dict[str, Any]:
        project_name = clean_text(name, 120)
        project_topic = clean_multiline(topic, 2_000)
        if not project_name:
            raise ValueError("项目名称不能为空。")
        if not project_topic:
            raise ValueError("研究主题不能为空。")
        project_id, timestamp = new_id("proj"), now()
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO projects(id,name,topic,query,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (project_id, project_name, project_topic, clean_multiline(query, 2_000), timestamp, timestamp),
            )
        return self.get_project(project_id)

    def list_projects(self) -> list[dict[str, Any]]:
        with self.store.connect() as db:
            rows = db.execute(
                """SELECT p.*, (SELECT COUNT(*) FROM papers WHERE project_id=p.id) paper_count,
                (SELECT COUNT(*) FROM gaps WHERE project_id=p.id) gap_count
                FROM projects p ORDER BY updated_at DESC"""
            ).fetchall()
        return [self._project_json(row) for row in rows]

    def get_project(self, project_id: str) -> dict[str, Any]:
        with self.store.connect() as db:
            row = db.execute(
                """SELECT p.*, (SELECT COUNT(*) FROM papers WHERE project_id=p.id) paper_count,
                (SELECT COUNT(*) FROM gaps WHERE project_id=p.id) gap_count
                FROM projects p WHERE id=?""", (project_id,),
            ).fetchone()
        if not row:
            raise KeyError("项目不存在。")
        return self._project_json(row)

    @staticmethod
    def _project_json(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "name": row["name"], "topic": row["topic"], "query": row["query"],
            "paperCount": int(row["paper_count"] if "paper_count" in row.keys() else 0),
            "gapCount": int(row["gap_count"] if "gap_count" in row.keys() else 0),
            "createdAt": row["created_at"], "updatedAt": row["updated_at"],
        }

    def update_project(self, project_id: str, data: dict[str, Any]) -> dict[str, Any]:
        current = self.get_project(project_id)
        name = clean_text(data.get("name", current["name"]), 120)
        topic = clean_multiline(data.get("topic", current["topic"]), 2_000)
        query = clean_multiline(data.get("query", current["query"]), 2_000)
        if not name or not topic:
            raise ValueError("项目名称和研究主题不能为空。")
        with self.store.connect() as db:
            db.execute("UPDATE projects SET name=?,topic=?,query=?,updated_at=? WHERE id=?", (name, topic, query, now(), project_id))
        return self.get_project(project_id)

    def delete_project(self, project_id: str, confirmation: str) -> None:
        project = self.get_project(project_id)
        if confirmation != project["name"]:
            raise ValueError("请输入完整项目名称以确认删除。")
        with self.store.connect() as db:
            db.execute("DELETE FROM projects WHERE id=?", (project_id,))
        target = self.root / "projects" / project_id
        if target.resolve().parent == (self.root / "projects").resolve():
            shutil.rmtree(target, ignore_errors=True)

    def import_semantic_paper(self, project_id: str, identifier: str) -> tuple[dict[str, Any], bool]:
        self.get_project(project_id)
        metadata = self.semantic_scholar.paper(identifier)
        if not metadata["paperId"] or not metadata["title"]:
            raise ValueError("论文元数据缺少稳定 ID 或标题。")
        source_key = f"s2:{metadata['paperId']}"
        with self.store.connect() as db:
            existing = db.execute("SELECT id FROM papers WHERE project_id=? AND source_key=?", (project_id, source_key)).fetchone()
        if existing:
            return self.get_paper(existing["id"], project_id), False
        paper_id = new_id("paper")
        source = self._acquire_semantic_source(project_id, paper_id, metadata)
        timestamp = now()
        with self.store.connect() as db:
            db.execute(
                """INSERT INTO papers(id,project_id,source_kind,source_key,semantic_scholar_id,doi,arxiv_id,
                title,abstract,authors_json,year,venue,external_url,pdf_url,citation_count,reference_count,
                evidence_level,content_path,locator_mode,status,error,metadata_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    paper_id, project_id, "semantic_scholar", source_key, metadata["paperId"], metadata["doi"],
                    metadata["arxivId"], metadata["title"], metadata["abstract"],
                    json.dumps(metadata["authors"], ensure_ascii=False), metadata["year"], metadata["venue"],
                    metadata["url"], metadata["pdfUrl"], metadata["citationCount"], metadata["referenceCount"],
                    source.evidence_level, source.content_path, source.locator_mode, "ready", source.warning,
                    json.dumps(metadata, ensure_ascii=False), timestamp, timestamp,
                ),
            )
            db.execute("UPDATE projects SET updated_at=? WHERE id=?", (timestamp, project_id))
        return self.get_paper(paper_id, project_id), True

    def _acquire_semantic_source(self, project_id: str, paper_id: str, metadata: dict[str, Any]) -> PaperSource:
        abstract = metadata.get("abstract") or ""
        if metadata.get("pdfUrl"):
            directory = self._paper_dir(project_id, paper_id)
            try:
                pdf_path = download_pdf(metadata["pdfUrl"], directory / "source.pdf")
                text, _ = extract_pdf_text(pdf_path)
                if len(clean_text(text, len(text) + 1)) >= 2_000:
                    text_path = directory / "source.txt"
                    text_path.write_text(text, encoding="utf-8")
                    return PaperSource(text, "fulltext", "page", str(text_path))
                warning = "开放 PDF 缺少可用文字层，已降级为摘要证据。"
            except Exception as exc:
                warning = f"开放 PDF 获取或解析失败，已降级为摘要证据：{clean_text(exc, 300)}"
        else:
            warning = "未找到开放 PDF，已使用摘要证据。"
        if not abstract:
            warning = f"{warning} 当前元数据也没有摘要。"
        return PaperSource(abstract, "abstract", "abstract", None, warning)

    def import_reader_paper(self, project_id: str, cache_key: str) -> tuple[dict[str, Any], bool]:
        self.get_project(project_id)
        payload = self.reader_source(cache_key)
        if not payload:
            raise ValueError("本地阅读器文档不存在或缓存已失效。")
        entry, source_path = payload["entry"], Path(payload["path"])
        source_key = f"reader:{cache_key}"
        with self.store.connect() as db:
            existing = db.execute("SELECT id FROM papers WHERE project_id=? AND source_key=?", (project_id, source_key)).fetchone()
        if existing:
            return self.get_paper(existing["id"], project_id), False
        paper_id = new_id("paper")
        text, locator_mode = extract_local_text(source_path, str(entry.get("renderKind") or ""))
        evidence_level = "fulltext"
        warning = None if len(clean_text(text, len(text) + 1)) >= 2_000 else "本地正文可提取内容较少，分析结论需人工核验。"
        directory = self._paper_dir(project_id, paper_id)
        directory.mkdir(parents=True, exist_ok=True)
        text_path = directory / "source.txt"
        text_path.write_text(text, encoding="utf-8")
        timestamp = now()
        with self.store.connect() as db:
            db.execute(
                """INSERT INTO papers(id,project_id,source_kind,source_key,reader_cache_key,title,authors_json,
                evidence_level,content_path,locator_mode,status,error,metadata_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    paper_id, project_id, "reader", source_key, cache_key, entry.get("title") or source_path.stem,
                    "[]", evidence_level, str(text_path), locator_mode, "ready",
                    warning,
                    json.dumps(entry, ensure_ascii=False), timestamp, timestamp,
                ),
            )
            db.execute("UPDATE projects SET updated_at=? WHERE id=?", (timestamp, project_id))
        return self.get_paper(paper_id, project_id), True

    def get_paper(self, paper_id: str, project_id: str | None = None) -> dict[str, Any]:
        query, values = "SELECT * FROM papers WHERE id=?", [paper_id]
        if project_id:
            query += " AND project_id=?"
            values.append(project_id)
        with self.store.connect() as db:
            row = db.execute(query, values).fetchone()
        if not row:
            raise KeyError("论文不存在。")
        return self._paper_json(row)

    def list_papers(self, project_id: str, search: str = "") -> list[dict[str, Any]]:
        self.get_project(project_id)
        query = "SELECT * FROM papers WHERE project_id=?"
        values: list[Any] = [project_id]
        if search:
            query += " AND (title LIKE ? OR authors_json LIKE ? OR venue LIKE ?)"
            term = f"%{search}%"
            values.extend([term, term, term])
        query += " ORDER BY COALESCE(year,0) DESC, created_at DESC"
        with self.store.connect() as db:
            rows = db.execute(query, values).fetchall()
            counts = {
                row["paper_id"]: row for row in db.execute(
                    """SELECT paper_id,
                    SUM(CASE WHEN kind='contribution' AND review_status!='rejected' THEN 1 ELSE 0 END) contribution_count,
                    SUM(CASE WHEN kind IN ('limitation','failure_condition') AND review_status!='rejected' THEN 1 ELSE 0 END) limitation_count
                    FROM facts WHERE project_id=? GROUP BY paper_id""", (project_id,)
                ).fetchall()
            }
        result = []
        for row in rows:
            item = self._paper_json(row)
            count = counts.get(row["id"])
            item["contributionCount"] = int(count["contribution_count"] or 0) if count else 0
            item["limitationCount"] = int(count["limitation_count"] or 0) if count else 0
            result.append(item)
        return result

    @staticmethod
    def _paper_json(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "projectId": row["project_id"], "sourceKind": row["source_kind"],
            "semanticScholarId": row["semantic_scholar_id"], "doi": row["doi"], "arxivId": row["arxiv_id"],
            "readerCacheKey": row["reader_cache_key"], "title": row["title"], "abstract": row["abstract"],
            "authors": json.loads(row["authors_json"] or "[]"), "year": row["year"], "venue": row["venue"],
            "url": row["external_url"], "pdfUrl": row["pdf_url"], "citationCount": row["citation_count"],
            "referenceCount": row["reference_count"], "evidenceLevel": row["evidence_level"],
            "locatorMode": row["locator_mode"], "status": row["status"], "error": row["error"],
            "analyzedAt": row["analyzed_at"], "createdAt": row["created_at"], "updatedAt": row["updated_at"],
        }

    def delete_paper(self, project_id: str, paper_id: str) -> None:
        self.get_paper(paper_id, project_id)
        with self.store.connect() as db:
            gap_ids = [row[0] for row in db.execute("SELECT DISTINCT gap_id FROM relations WHERE paper_id=?", (paper_id,))]
            db.execute("DELETE FROM papers WHERE id=? AND project_id=?", (paper_id, project_id))
            db.execute("DELETE FROM gaps WHERE project_id=? AND id NOT IN (SELECT DISTINCT gap_id FROM relations)", (project_id,))
        for gap_id in gap_ids:
            self.recompute_gap(gap_id)
        target = self._paper_dir(project_id, paper_id)
        if target.resolve().parent == (self.root / "projects" / project_id / "papers").resolve():
            shutil.rmtree(target, ignore_errors=True)

    def submit_analysis(self, project_id: str, paper_ids: list[str], preset_id: str) -> dict[str, Any]:
        self.get_project(project_id)
        available = {item["id"] for item in self.llm_presets()}
        if preset_id not in available:
            raise ValueError("请选择有效的 LLM 预设。")
        unique_ids = list(dict.fromkeys(clean_text(item, 100) for item in paper_ids if clean_text(item, 100)))
        if not unique_ids:
            unique_ids = [item["id"] for item in self.list_papers(project_id)]
        if not unique_ids:
            raise ValueError("项目中还没有可分析的论文。")
        for paper_id in unique_ids:
            self.get_paper(paper_id, project_id)
        job_id, timestamp = new_id("job"), now()
        with self.store.connect() as db:
            db.execute(
                """INSERT INTO analysis_jobs(id,project_id,paper_ids_json,preset_id,status,stage,total,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (job_id, project_id, json.dumps(unique_ids), preset_id, "queued", "queued", len(unique_ids), timestamp, timestamp),
            )
        self.executor.submit(self._run_analysis, job_id, project_id, unique_ids, preset_id)
        return self.get_job(job_id)

    def _job_update(self, job_id: str, **values: Any) -> None:
        if not values:
            return
        values["updated_at"] = now()
        assignments = ",".join(f"{key}=?" for key in values)
        with self.store.connect() as db:
            db.execute(f"UPDATE analysis_jobs SET {assignments} WHERE id=?", [*values.values(), job_id])

    def _job_log(self, job_id: str, message: str) -> None:
        with self.store.connect() as db:
            row = db.execute("SELECT logs_json FROM analysis_jobs WHERE id=?", (job_id,)).fetchone()
            logs = json.loads(row["logs_json"] or "[]") if row else []
            logs.append(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {message}")
            db.execute("UPDATE analysis_jobs SET logs_json=?,updated_at=? WHERE id=?", (json.dumps(logs[-200:], ensure_ascii=False), now(), job_id))

    def _run_analysis(self, job_id: str, project_id: str, paper_ids: list[str], preset_id: str) -> None:
        self._job_update(job_id, status="running", stage="extracting")
        try:
            for index, paper_id in enumerate(paper_ids):
                paper = self.get_paper(paper_id, project_id)
                self._job_update(job_id, current_paper_id=paper_id, stage="extracting", completed=index)
                self._job_log(job_id, f"正在分析：{paper['title']}")
                self._analyze_paper(project_id, paper, preset_id, job_id)
                self._job_update(job_id, completed=index + 1)
            self._job_update(job_id, status="completed", stage="completed", current_paper_id=None, finished_at=now())
            self._job_log(job_id, "分析完成。")
        except Exception as exc:
            self._job_update(job_id, status="failed", stage="failed", error=clean_text(exc, 1_000), finished_at=now())
            self._job_log(job_id, f"分析失败：{clean_text(exc, 500)}")

    def _paper_text(self, paper: dict[str, Any]) -> str:
        with self.store.connect() as db:
            row = db.execute("SELECT content_path,abstract FROM papers WHERE id=?", (paper["id"],)).fetchone()
        if row and row["content_path"]:
            path = Path(row["content_path"])
            if path.is_file() and self.root.resolve() in path.resolve().parents:
                return path.read_text(encoding="utf-8", errors="replace")[:MAX_ANALYSIS_CHARS]
        return clean_multiline(row["abstract"] if row else paper.get("abstract"), MAX_ANALYSIS_CHARS)

    def _analyze_paper(self, project_id: str, paper: dict[str, Any], preset_id: str, job_id: str) -> None:
        source_text = self._paper_text(paper)
        if not clean_text(source_text):
            raise ValueError(f"{paper['title']} 没有可分析的正文或摘要。")
        candidates: list[dict[str, Any]] = []
        chunks = split_text(source_text)
        for index, chunk in enumerate(chunks):
            self._job_update(job_id, stage=f"extracting {index + 1}/{len(chunks)}")
            response = self.llm_request(preset_id, self._extraction_messages(paper, chunk, index + 1, len(chunks)))
            payload = extract_json_object(response)
            candidates.extend(self._validated_candidates(payload, chunk, paper["evidenceLevel"]))
        if candidates:
            self._job_update(job_id, stage="consolidating")
            response = self.llm_request(preset_id, self._consolidation_messages(paper, candidates))
            consolidated = self._validated_candidates(extract_json_object(response), source_text, paper["evidenceLevel"])
            if consolidated:
                candidates = consolidated
        self._job_update(job_id, stage="normalizing")
        with self.store.connect() as db:
            db.execute("DELETE FROM facts WHERE paper_id=?", (paper["id"],))
            db.execute("DELETE FROM relations WHERE paper_id=?", (paper["id"],))
            timestamp = now()
            for item in candidates:
                db.execute(
                    """INSERT INTO facts(id,project_id,paper_id,kind,title,detail,confidence,evidence,locator,review_status,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (new_id("fact"), project_id, paper["id"], item["kind"], item["title"], item["detail"],
                     item["confidence"], item["evidence"], item["locator"], "pending", timestamp, timestamp),
                )
        gap_candidates = [item for item in candidates if item["kind"] == "gap"]
        self._normalize_gaps(project_id, paper, gap_candidates, preset_id)
        with self.store.connect() as db:
            db.execute("UPDATE papers SET status='analyzed',analyzed_at=?,updated_at=?,error=NULL WHERE id=?", (now(), now(), paper["id"]))
            db.execute("DELETE FROM gaps WHERE project_id=? AND id NOT IN (SELECT DISTINCT gap_id FROM relations)", (project_id,))
        for gap in self.list_gaps(project_id):
            self.recompute_gap(gap["id"])

    @staticmethod
    def _extraction_messages(paper: dict[str, Any], chunk: str, index: int, total: int) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": (
                "你是严谨的科研证据抽取器。只依据给定论文文本，不使用外部知识，不猜测作者未声明的结论。"
                "只输出一个 JSON 对象，格式为 {\"items\":[{\"kind\":\"problem|task|method|dataset|metric|"
                "contribution|limitation|failure_condition|gap\",\"title\":\"中文短标题\",\"detail\":\"中文说明\","
                "\"confidence\":0到1,\"evidence\":\"原文连续逐字证据\",\"locator\":\"Page/Block/Section\","
                "\"relationType\":\"proposes|addresses|partially_solves|solves|fails_on\"}]}。"
                "gap 条目必须有 relationType；局限或尚未解决的问题通常为 fails_on。证据必须在输入中原样出现。"
            )},
            {"role": "user", "content": (
                f"论文：{paper['title']}\n年份：{paper.get('year') or '未知'}\n"
                f"证据等级：{paper['evidenceLevel']}\n文本块：{index}/{total}\n\n{chunk}"
            )},
        ]

    @staticmethod
    def _consolidation_messages(paper: dict[str, Any], items: list[dict[str, Any]]) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": (
                "合并同一篇论文的重复科研事实。只输出 {\"items\":[...]} JSON，字段与输入完全相同。"
                "保留最具体的中文标题和说明；evidence 与 locator 必须从输入条目逐字复制，不得改写或新增证据。"
            )},
            {"role": "user", "content": f"论文：{paper['title']}\n\n候选事实：\n{json.dumps({'items': items}, ensure_ascii=False)}"},
        ]

    @staticmethod
    def _validated_candidates(payload: dict[str, Any], source: str, evidence_level: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for raw in payload.get("items", []):
            if not isinstance(raw, dict):
                continue
            kind = clean_text(raw.get("kind"), 50).lower()
            title = clean_text(raw.get("title"), 300)
            evidence = clean_text(raw.get("evidence"), 2_000)
            if kind not in VALID_FACT_KINDS or not title or not text_contains_evidence(source, evidence):
                continue
            relation = clean_text(raw.get("relationType"), 50).lower()
            if kind == "gap" and relation not in VALID_RELATIONS:
                relation = "fails_on"
            try:
                confidence = max(0.0, min(1.0, float(raw.get("confidence", 0.5))))
            except (TypeError, ValueError):
                confidence = 0.5
            if evidence_level == "abstract":
                confidence = min(confidence, 0.65)
            result.append({
                "kind": kind, "title": title, "detail": clean_multiline(raw.get("detail"), 4_000),
                "confidence": confidence, "evidence": evidence,
                "locator": clean_text(raw.get("locator"), 300) or ("Abstract" if evidence_level == "abstract" else "正文"),
                "relationType": relation,
            })
        return result[:120]

    def _normalize_gaps(self, project_id: str, paper: dict[str, Any], candidates: list[dict[str, Any]], preset_id: str) -> None:
        if not candidates:
            return
        existing = self.list_gaps(project_id)
        assignments: list[dict[str, Any]] = []
        if existing:
            messages = [
                {"role": "system", "content": (
                    "将候选研究空白归并到同一项目的规范空白。只输出 JSON："
                    "{\"assignments\":[{\"candidateIndex\":0,\"gapId\":\"已有ID或null\","
                    "\"canonicalTitle\":\"中文规范标题\",\"description\":\"中文说明\",\"domain\":\"领域\","
                    "\"task\":\"任务\",\"failureCondition\":\"失败条件\",\"relationType\":\"五种关系之一\","
                    "\"confidence\":0到1,\"rationale\":\"归并理由\"}]}。只有语义与失败条件实质相同才复用 gapId。"
                )},
                {"role": "user", "content": json.dumps({
                    "paper": {"title": paper["title"], "year": paper.get("year")},
                    "existingGaps": [{"id": item["id"], "title": item["canonicalTitle"], "description": item["description"], "failureCondition": item["failureCondition"]} for item in existing],
                    "candidates": candidates,
                }, ensure_ascii=False)},
            ]
            try:
                payload = extract_json_object(self.llm_request(preset_id, messages))
                assignments = [item for item in payload.get("assignments", []) if isinstance(item, dict)]
            except Exception:
                assignments = []
        by_index = {int(item.get("candidateIndex")): item for item in assignments if str(item.get("candidateIndex", "")).isdigit()}
        existing_ids = {item["id"] for item in existing}
        with self.store.connect() as db:
            timestamp = now()
            for index, candidate in enumerate(candidates):
                assignment = by_index.get(index, {})
                gap_id = clean_text(assignment.get("gapId"), 100)
                if gap_id not in existing_ids:
                    gap_id = new_id("gap")
                    canonical = clean_text(assignment.get("canonicalTitle"), 300) or candidate["title"]
                    db.execute(
                        """INSERT INTO gaps(id,project_id,canonical_title,description,domain,task,failure_condition,status,provisional,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (gap_id, project_id, canonical, clean_multiline(assignment.get("description"), 4_000) or candidate["detail"],
                         clean_text(assignment.get("domain"), 300), clean_text(assignment.get("task"), 500),
                         clean_multiline(assignment.get("failureCondition"), 2_000) or candidate["detail"],
                         "OPEN", 1, timestamp, timestamp),
                    )
                    existing_ids.add(gap_id)
                relation_type = clean_text(assignment.get("relationType"), 50).lower()
                if relation_type not in VALID_RELATIONS:
                    relation_type = candidate["relationType"] if candidate["relationType"] in VALID_RELATIONS else "fails_on"
                try:
                    confidence = max(0.0, min(1.0, float(assignment.get("confidence", candidate["confidence"]))))
                except (TypeError, ValueError):
                    confidence = candidate["confidence"]
                db.execute(
                    """INSERT OR IGNORE INTO relations(id,project_id,paper_id,gap_id,relation_type,confidence,evidence,locator,rationale,review_status,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (new_id("rel"), project_id, paper["id"], gap_id, relation_type, confidence,
                     candidate["evidence"], candidate["locator"], clean_multiline(assignment.get("rationale"), 2_000),
                     "pending", timestamp, timestamp),
                )

    def recompute_gap(self, gap_id: str) -> dict[str, Any] | None:
        with self.store.connect() as db:
            gap = db.execute("SELECT * FROM gaps WHERE id=?", (gap_id,)).fetchone()
            if not gap:
                return None
            rows = db.execute(
                """SELECT r.*, p.year FROM relations r JOIN papers p ON p.id=r.paper_id
                WHERE r.gap_id=? AND r.review_status!='rejected'""", (gap_id,),
            ).fetchall()
            positive = [row for row in rows if row["relation_type"] in {"addresses", "partially_solves", "solves"}]
            solve_papers = {row["paper_id"] for row in rows if row["relation_type"] == "solves"}
            solve_years = [row["year"] for row in rows if row["relation_type"] == "solves" and row["year"]]
            fail_years = [row["year"] for row in rows if row["relation_type"] == "fails_on" and row["year"]]
            newer_failure = bool(solve_years and fail_years and max(fail_years) > max(solve_years))
            if not positive:
                status = "OPEN"
            elif len(solve_papers) >= 2 and not newer_failure:
                status = "MATURE"
            else:
                status = "PARTIALLY_ADDRESSED"
            provisional = int(any(row["review_status"] == "pending" for row in rows))
            db.execute("UPDATE gaps SET status=?,provisional=?,updated_at=? WHERE id=?", (status, provisional, now(), gap_id))
        return self.get_gap(gap_id)

    def get_gap(self, gap_id: str) -> dict[str, Any]:
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM gaps WHERE id=?", (gap_id,)).fetchone()
            if not row:
                raise KeyError("研究空白不存在。")
            relations = db.execute(
                """SELECT r.*,p.title paper_title,p.year,p.evidence_level FROM relations r
                JOIN papers p ON p.id=r.paper_id WHERE r.gap_id=? ORDER BY p.year DESC,r.created_at DESC""", (gap_id,),
            ).fetchall()
        return self._gap_json(row, relations)

    def list_gaps(self, project_id: str, status: str = "", review: str = "", search: str = "", sort: str = "updated") -> list[dict[str, Any]]:
        self.get_project(project_id)
        query = "SELECT * FROM gaps WHERE project_id=?"
        values: list[Any] = [project_id]
        if status in {"OPEN", "PARTIALLY_ADDRESSED", "MATURE"}:
            query += " AND status=?"; values.append(status)
        if search:
            query += " AND (canonical_title LIKE ? OR description LIKE ? OR task LIKE ?)"
            term = f"%{search}%"; values.extend([term, term, term])
        query += " ORDER BY canonical_title COLLATE NOCASE" if sort == "title" else " ORDER BY updated_at DESC"
        with self.store.connect() as db:
            gaps = db.execute(query, values).fetchall()
            relation_rows = db.execute(
                """SELECT r.*,p.title paper_title,p.year,p.evidence_level FROM relations r
                JOIN papers p ON p.id=r.paper_id WHERE r.project_id=? ORDER BY p.year DESC,r.created_at DESC""", (project_id,),
            ).fetchall()
        by_gap: dict[str, list[sqlite3.Row]] = {}
        for row in relation_rows:
            by_gap.setdefault(row["gap_id"], []).append(row)
        items = [self._gap_json(row, by_gap.get(row["id"], [])) for row in gaps]
        if review in VALID_REVIEWS:
            items = [item for item in items if any(rel["reviewStatus"] == review for rel in item["relations"])]
        if sort == "confidence":
            items.sort(key=lambda item: item["confidence"], reverse=True)
        elif sort == "evidence":
            items.sort(key=lambda item: item["evidenceCount"], reverse=True)
        return items

    @staticmethod
    def _gap_json(row: sqlite3.Row, relations: list[sqlite3.Row]) -> dict[str, Any]:
        relation_items = [{
            "id": rel["id"], "paperId": rel["paper_id"], "paperTitle": rel["paper_title"], "paperYear": rel["year"],
            "relationType": rel["relation_type"], "confidence": rel["confidence"], "evidence": rel["evidence"],
            "locator": rel["locator"], "rationale": rel["rationale"], "reviewStatus": rel["review_status"],
            "evidenceLevel": rel["evidence_level"], "updatedAt": rel["updated_at"],
        } for rel in relations]
        active = [rel for rel in relation_items if rel["reviewStatus"] != "rejected"]
        confidence = sum(float(rel["confidence"]) for rel in active) / len(active) if active else 0
        return {
            "id": row["id"], "projectId": row["project_id"], "canonicalTitle": row["canonical_title"],
            "description": row["description"], "domain": row["domain"], "task": row["task"],
            "failureCondition": row["failure_condition"], "status": row["status"],
            "provisional": bool(row["provisional"]), "confidence": round(confidence, 3),
            "evidenceCount": len(active), "relations": relation_items,
            "createdAt": row["created_at"], "updatedAt": row["updated_at"],
        }

    def review_queue(self, project_id: str, search: str = "") -> list[dict[str, Any]]:
        self.get_project(project_id)
        with self.store.connect() as db:
            facts = db.execute(
                """SELECT f.*,p.title paper_title,p.evidence_level FROM facts f JOIN papers p ON p.id=f.paper_id
                WHERE f.project_id=? AND f.review_status='pending' ORDER BY f.created_at DESC""", (project_id,),
            ).fetchall()
            relations = db.execute(
                """SELECT r.*,p.title paper_title,p.evidence_level,g.canonical_title gap_title FROM relations r
                JOIN papers p ON p.id=r.paper_id JOIN gaps g ON g.id=r.gap_id
                WHERE r.project_id=? AND r.review_status='pending' ORDER BY r.created_at DESC""", (project_id,),
            ).fetchall()
        result = [{
            "id": row["id"], "itemType": "fact", "paperId": row["paper_id"], "paperTitle": row["paper_title"],
            "kind": row["kind"], "title": row["title"], "detail": row["detail"], "confidence": row["confidence"],
            "evidence": row["evidence"], "locator": row["locator"], "evidenceLevel": row["evidence_level"],
            "reviewStatus": row["review_status"],
        } for row in facts]
        result.extend({
            "id": row["id"], "itemType": "relation", "paperId": row["paper_id"], "paperTitle": row["paper_title"],
            "gapId": row["gap_id"], "gapTitle": row["gap_title"], "relationType": row["relation_type"],
            "title": row["gap_title"], "detail": row["rationale"], "confidence": row["confidence"],
            "evidence": row["evidence"], "locator": row["locator"], "evidenceLevel": row["evidence_level"],
            "reviewStatus": row["review_status"],
        } for row in relations)
        if search:
            needle = search.casefold()
            result = [item for item in result if needle in json.dumps(item, ensure_ascii=False).casefold()]
        return result

    def update_fact(self, fact_id: str, data: dict[str, Any]) -> dict[str, Any]:
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM facts WHERE id=?", (fact_id,)).fetchone()
            if not row:
                raise KeyError("证据事实不存在。")
            review = clean_text(data.get("reviewStatus", row["review_status"]), 30)
            kind = clean_text(data.get("kind", row["kind"]), 50)
            if review not in VALID_REVIEWS or kind not in VALID_FACT_KINDS:
                raise ValueError("审核状态或事实类型无效。")
            db.execute(
                "UPDATE facts SET kind=?,title=?,detail=?,review_status=?,updated_at=? WHERE id=?",
                (kind, clean_text(data.get("title", row["title"]), 300), clean_multiline(data.get("detail", row["detail"]), 4_000), review, now(), fact_id),
            )
            updated = db.execute("SELECT * FROM facts WHERE id=?", (fact_id,)).fetchone()
        return dict(updated)

    def update_relation(self, relation_id: str, data: dict[str, Any]) -> dict[str, Any]:
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM relations WHERE id=?", (relation_id,)).fetchone()
            if not row:
                raise KeyError("论文—空白关系不存在。")
            review = clean_text(data.get("reviewStatus", row["review_status"]), 30)
            relation = clean_text(data.get("relationType", row["relation_type"]), 50)
            if review not in VALID_REVIEWS or relation not in VALID_RELATIONS:
                raise ValueError("审核状态或关系类型无效。")
            db.execute(
                "UPDATE relations SET relation_type=?,rationale=?,review_status=?,updated_at=? WHERE id=?",
                (relation, clean_multiline(data.get("rationale", row["rationale"]), 2_000), review, now(), relation_id),
            )
            gap_id = row["gap_id"]
        self.recompute_gap(gap_id)
        return self.get_gap(gap_id)

    def update_gap(self, gap_id: str, data: dict[str, Any]) -> dict[str, Any]:
        current = self.get_gap(gap_id)
        title = clean_text(data.get("canonicalTitle", current["canonicalTitle"]), 300)
        if not title:
            raise ValueError("研究空白标题不能为空。")
        with self.store.connect() as db:
            db.execute(
                """UPDATE gaps SET canonical_title=?,description=?,domain=?,task=?,failure_condition=?,updated_at=? WHERE id=?""",
                (title, clean_multiline(data.get("description", current["description"]), 4_000),
                 clean_text(data.get("domain", current["domain"]), 300), clean_text(data.get("task", current["task"]), 500),
                 clean_multiline(data.get("failureCondition", current["failureCondition"]), 2_000), now(), gap_id),
            )
        return self.get_gap(gap_id)

    def graph(self, project_id: str) -> dict[str, Any]:
        papers = self.list_papers(project_id)
        gaps = self.list_gaps(project_id)
        with self.store.connect() as db:
            facts = db.execute(
                "SELECT * FROM facts WHERE project_id=? AND review_status!='rejected' AND kind IN ('contribution','limitation','failure_condition')",
                (project_id,),
            ).fetchall()
        nodes = [{"id": paper["id"], "type": "paper", "label": paper["title"], "status": paper["status"]} for paper in papers]
        nodes.extend({"id": gap["id"], "type": "gap", "label": gap["canonicalTitle"], "status": gap["status"]} for gap in gaps)
        nodes.extend({
            "id": row["id"], "type": row["kind"], "label": row["title"], "status": row["review_status"],
            "paperId": row["paper_id"], "detail": row["detail"], "evidence": row["evidence"],
            "locator": row["locator"], "confidence": row["confidence"],
        } for row in facts)
        edges: list[dict[str, Any]] = []
        facts_by_paper: dict[str, list[sqlite3.Row]] = {}
        for row in facts:
            facts_by_paper.setdefault(row["paper_id"], []).append(row)
            edges.append({"id": f"paper-{row['id']}", "source": row["paper_id"], "target": row["id"], "type": row["kind"]})
        for gap in gaps:
            for relation in gap["relations"]:
                if relation["reviewStatus"] == "rejected":
                    continue
                paper_facts = facts_by_paper.get(relation["paperId"], [])
                preferred_kinds = {"contribution"} if relation["relationType"] in {"proposes", "addresses", "partially_solves", "solves"} else {"limitation", "failure_condition"}
                source_fact = next((item for item in paper_facts if item["kind"] in preferred_kinds), None)
                edges.append({
                    "id": relation["id"],
                    "source": source_fact["id"] if source_fact else relation["paperId"],
                    "target": gap["id"],
                    "type": relation["relationType"],
                })
        return {"nodes": nodes, "edges": edges}

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM analysis_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise KeyError("分析任务不存在。")
        return {
            "id": row["id"], "projectId": row["project_id"], "paperIds": json.loads(row["paper_ids_json"]),
            "presetId": row["preset_id"], "status": row["status"], "stage": row["stage"],
            "completed": row["completed"], "total": row["total"], "currentPaperId": row["current_paper_id"],
            "logs": json.loads(row["logs_json"] or "[]"), "error": row["error"],
            "createdAt": row["created_at"], "updatedAt": row["updated_at"], "finishedAt": row["finished_at"],
            "retryable": row["status"] in {"failed", "interrupted"},
        }

    def export_project(self, project_id: str) -> dict[str, Any]:
        project = self.get_project(project_id)
        with self.store.connect() as db:
            facts = [dict(row) for row in db.execute("SELECT * FROM facts WHERE project_id=? ORDER BY created_at", (project_id,))]
        return {
            "schemaVersion": 1, "exportedAt": datetime.now(timezone.utc).isoformat(), "project": project,
            "papers": self.list_papers(project_id), "gaps": self.list_gaps(project_id), "facts": facts,
            "graph": self.graph(project_id),
        }


def create_research_gap_blueprint(service: ResearchGapService) -> Blueprint:
    bp = Blueprint("research_gaps_api", __name__, url_prefix="/api/research-gaps")

    def body() -> dict[str, Any]:
        value = request.get_json(silent=True)
        return value if isinstance(value, dict) else {}

    def error_response(exc: Exception):
        status = 404 if isinstance(exc, KeyError) else 400 if isinstance(exc, ValueError) else 502 if isinstance(exc, requests.RequestException) else 503 if isinstance(exc, RuntimeError) else 500
        message = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
        return jsonify({"error": message or "请求失败。"}), status

    @bp.get("/config")
    def config():
        return jsonify({"llmPresets": service.llm_presets(), "semanticScholarApiKeyConfigured": bool(os.getenv("SEMANTIC_SCHOLAR_API_KEY", "").strip())})

    @bp.get("/projects")
    def projects_list():
        return jsonify({"projects": service.list_projects()})

    @bp.post("/projects")
    def projects_create():
        data = body()
        try:
            return jsonify({"project": service.create_project(data.get("name"), data.get("topic"), data.get("query"))}), 201
        except Exception as exc:
            return error_response(exc)

    @bp.get("/projects/<project_id>")
    def projects_get(project_id: str):
        try:
            return jsonify({"project": service.get_project(project_id)})
        except Exception as exc:
            return error_response(exc)

    @bp.patch("/projects/<project_id>")
    def projects_update(project_id: str):
        try:
            return jsonify({"project": service.update_project(project_id, body())})
        except Exception as exc:
            return error_response(exc)

    @bp.delete("/projects/<project_id>")
    def projects_delete(project_id: str):
        try:
            service.delete_project(project_id, clean_text(body().get("confirmName"), 120))
            return jsonify({"deleted": True})
        except Exception as exc:
            return error_response(exc)

    @bp.get("/search")
    def search():
        query = clean_text(request.args.get("q"), 1_000)
        if not query:
            return jsonify({"error": "请输入标题、DOI 或 arXiv 标识。"}), 400
        try:
            return jsonify({"results": service.semantic_scholar.search(query), "exact": parse_external_identifier(query) is not None})
        except Exception as exc:
            return error_response(exc)

    @bp.get("/projects/<project_id>/papers")
    def papers_list(project_id: str):
        try:
            return jsonify({"papers": service.list_papers(project_id, clean_text(request.args.get("q"), 300))})
        except Exception as exc:
            return error_response(exc)

    @bp.post("/projects/<project_id>/papers")
    def papers_create(project_id: str):
        data = body()
        try:
            if data.get("readerCacheKey"):
                paper, created = service.import_reader_paper(project_id, clean_text(data["readerCacheKey"], 100))
            else:
                identifier = clean_text(data.get("paperId") or data.get("identifier"), 500)
                if not identifier:
                    raise ValueError("请选择 Semantic Scholar 论文或本地阅读器文档。")
                paper, created = service.import_semantic_paper(project_id, identifier)
            return jsonify({"paper": paper, "created": created}), 201 if created else 200
        except Exception as exc:
            return error_response(exc)

    @bp.delete("/projects/<project_id>/papers/<paper_id>")
    def papers_delete(project_id: str, paper_id: str):
        try:
            service.delete_paper(project_id, paper_id)
            return jsonify({"deleted": True})
        except Exception as exc:
            return error_response(exc)

    @bp.post("/projects/<project_id>/analysis")
    def analysis_create(project_id: str):
        data = body()
        try:
            paper_ids = data.get("paperIds") if isinstance(data.get("paperIds"), list) else []
            return jsonify({"job": service.submit_analysis(project_id, paper_ids, clean_text(data.get("presetId"), 100))}), 202
        except Exception as exc:
            return error_response(exc)

    @bp.get("/jobs/<job_id>")
    def jobs_get(job_id: str):
        try:
            return jsonify({"job": service.get_job(job_id)})
        except Exception as exc:
            return error_response(exc)

    @bp.get("/projects/<project_id>/gaps")
    def gaps_list(project_id: str):
        try:
            return jsonify({"gaps": service.list_gaps(
                project_id, clean_text(request.args.get("status"), 40), clean_text(request.args.get("review"), 40),
                clean_text(request.args.get("q"), 300), clean_text(request.args.get("sort"), 40),
            )})
        except Exception as exc:
            return error_response(exc)

    @bp.get("/projects/<project_id>/review-queue")
    def review_queue(project_id: str):
        try:
            return jsonify({"items": service.review_queue(project_id, clean_text(request.args.get("q"), 300))})
        except Exception as exc:
            return error_response(exc)

    @bp.get("/projects/<project_id>/graph")
    def graph(project_id: str):
        try:
            return jsonify(service.graph(project_id))
        except Exception as exc:
            return error_response(exc)

    @bp.patch("/facts/<fact_id>")
    def facts_update(fact_id: str):
        try:
            return jsonify({"fact": service.update_fact(fact_id, body())})
        except Exception as exc:
            return error_response(exc)

    @bp.patch("/relations/<relation_id>")
    def relations_update(relation_id: str):
        try:
            return jsonify({"gap": service.update_relation(relation_id, body())})
        except Exception as exc:
            return error_response(exc)

    @bp.patch("/gaps/<gap_id>")
    def gaps_update(gap_id: str):
        try:
            return jsonify({"gap": service.update_gap(gap_id, body())})
        except Exception as exc:
            return error_response(exc)

    @bp.get("/projects/<project_id>/export")
    def projects_export(project_id: str):
        try:
            payload = service.export_project(project_id)
            filename = re.sub(r"[^A-Za-z0-9._-]+", "_", payload["project"]["name"]).strip("_") or "research-gaps"
            return Response(
                json.dumps(payload, ensure_ascii=False, indent=2), mimetype="application/json",
                headers={"Content-Disposition": f'attachment; filename="{filename}.json"'},
            )
        except Exception as exc:
            return error_response(exc)

    return bp
