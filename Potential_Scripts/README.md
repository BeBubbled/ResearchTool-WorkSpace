# **总体思路(由AI总结, 核心使用底部我推荐的代码)**

- 输入目录 -i/--dir 中选取按文件名排序的前 rows*cols 个视频（不足则复用最后一个）。

- 探测所有视频分辨率 → 统一上采样到最高分辨率（面积最大）。

- 依据模式：

  

  - **h（行主序）**：逐行从左到右布置；每格标题在**该行上方带**；**行 caption**在该行**下方带**；行 caption 的可用宽度被限制为 max(2, cols-3)*base_w（不含列间 gap），并**居中**。
  - **v（列主序）**：逐列从上到下布置；每格标题同上；**列 caption**放在整幅图像**最底部新开一条带**，按列居中。

  

- 画布白底（fill=white），文字黑色（默认 Arial，未给 --fontfile 时）。

- 标题与 caption 使用 **Pillow 先断行**，再通过 drawtext=textfile=... 读取，保证多行稳定呈现。

- 输出 MP4（H.264，yuv420p，crf=18，faststart）；分辨率强制为偶数。



# **readme.txt（严格）**

以 N = rows * cols：



1. 前 **N 行**：每格标题（顺序与 h/v 主序一致）。

2. **紧接两行空行**（分隔）。

3. 若 **h**：再给 **rows** 行，分别是每一行的 caption（可留空行表示该行空标题）。

   若 **v**：再给 **cols** 行，分别是每一列的 caption（可留空行表示该列空标题）。



比如./videos文件夹中有16个视频,

若h拼接,想要2行8列, 前16行为每个视频的小标题, 接两个空白行, 接2行row 标题

若v拼接,想要2行8列, 前16行为每个视频的小标题, 接两个空白行, 接8行row 标题





# **关键参数与注意点**

## **共同**

- -i/--dir：视频目录（readme.txt 默认就在该目录）。
- --rows/--cols：网格布局。
- -m/--mode: h（行主序）或 v（列主序）。
- -o/--out：输出 MP4 路径；若重名自动追加 _1,_2,...。
- --gap-px（默认 5）：格间距。
- --outer-border-px（默认 5）：外侧留白。
- --title-band-px（默认 40）：每格上方标题带高度。
- --title-fontsize（默认 22）：每格标题字号。
- --fontfile：如需中文无乱码，建议指向系统的中文 TrueType/OTF 字体。





## **仅h（行主序）**

- **行 caption 带**：--rowcap-band-px（默认 48；会根据行 caption 实际换行高度**动态增大**）。
- **列 caption 带**：--colcap-band-px（默认 0；一般不需要列带）。
- **行 caption 宽度约束**：实际换行与绘制都按 max(2, cols-3)*base_w - 10 像素宽（左右各 5px 内边距），并**居中**到该行。



## **仅 v（列主序）**

- **列 caption 带**：--colcap-band-px（默认 48；会根据列 caption 实际换行高度**动态增大**）。
- **行 caption 带**：--rowcap-band-px（默认 0；一般不需要行带）。



# **我对参数取值的建议**





- **h 模式**：如果确实需要很长的行 caption，请把 --rowcap-band-px 设大一些（代码仍会按需要再增高），--colcap-band-px 通常设为 0。
- **v 模式**：长列 caption 时，把 --colcap-band-px 设大一些（代码仍会按需要再增高），--rowcap-band-px 设为 0。







# 我推荐的命令(已配置好文字 视频间隔 默认Arial字体)



## **v 拼接（列主序，只有列 caption）**



```
python stack_cli.py videos \
  -i "./videos" \
  --rows 4 --cols 1 \
  -m v \
  --rowcap-band-px 0 \
  --colcap-band-px 300 \
  -o out.mp4
```

说明：列 caption 带给了 300px 的初始高度；若文本更高，会再自适应增高。



## **h 拼接（行主序，只有行 caption）**



```
python stack_cli.py videos \
  -i "./videos" \
  --rows 1 --cols 4 \
  -m h \
  --title-band-px 50 \
  --rowcap-band-px 200 \
  --colcap-band-px 0 \
  -o out.mp4
```

说明：行 caption 带给了 200px 的初始高度；实际会按“受限宽度”断行并**动态增高**到不裁切。列带关闭（0）即可。

