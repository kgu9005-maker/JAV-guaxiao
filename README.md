# 视频自动整理工具（JAV / FC2 / 欧美）

扫描文件夹里的视频文件，自动提取番号、抓取元数据（标题、封面、演员等），生成 Kodi / Plex / Jellyfin / Emby 兼容的媒体库文件，并按番号归档整理。

## 功能特性

- **JAV 有码**：从文件名提取番号，抓取 javbus 元数据（多线程并发，带随机延迟防限流）
- **FC2 无码**：fc2ppvdb → FC2 官网 → javten(FC2hub) → njavtv → 123av → javbus 无码区，多源链式兜底
- **欧美**：ThePornDB API（可选，需 Key）→ IAFD 免登录兜底（自动识别 Brazzers / Blacked / Vixen 等日期式命名）
- **生成媒体库文件**：`movie.nfo` + 竖版 `poster.jpg` + `fanart.jpg` + 演员头像（需 Pillow，自动安装）
- **自动归档**：按番号建文件夹，视频与刮削文件剪切进去
  - 成功 → 目标文件夹下的 `完成/`
  - 失败 → 目标文件夹下的 `失败/`（可重复跑，只处理剩余视频）

## 环境要求

- Windows 10/11
- Python 3.8+（安装时勾选 **Add Python to PATH**）
- 可选：Pillow（首次运行 bat 会自动通过清华镜像安装，用于生成竖版海报）

## 使用方法

### 方式一：双击运行（最简单）

1. 把 `整理视频.bat` 和 `jav_organizer.py` 放在同一个文件夹
2. 把视频文件也放进这个文件夹（或其子文件夹）
3. 双击 `整理视频.bat`

### 方式二：拖拽文件夹

把要整理的文件夹直接**拖到 `整理视频.bat` 图标上**，会整理那个文件夹里的视频，而 bat 自身所在文件夹不受影响。

### 方式三：命令行

```bat
整理视频.bat "D:\Downloads\videos" 6
```

第一个参数 = 目标文件夹，第二个参数 = 并发数。

## 并发数（速度 vs 风险）

| 并发 | 说明 |
|------|------|
| 4 | 默认，稳定够快（推荐） |
| 6 | 明显更快，javbus 一般扛得住 |
| 8 | 激进，容易被反爬限流；出现大批失败就退回 4~6 |

临时修改：编辑 bat 里的 `JAV_WORKERS=4`，或用命令行第二个参数。

## 代理设置（FC2 必看）

fc2ppvdb / FC2 官网在国内直连不通，需要走代理。bat 会**自动探测**本机常见代理端口（7897 / 7890 / 7891 / 10809 / 10808 / 1080 / 8888），检测到就直接用。

手动指定：编辑 bat，取消这一行的注释并改成你的端口：

```bat
set "JAV_PROXY=http://127.0.0.1:7897"
```

没检测到代理也不影响 JAV 有码刮削，只有 FC2 会失败。

## 可选高级配置（编辑 jav_organizer.py 顶部）

- `ENABLE_WESTERN = True`：开启欧美片刮削
- `TPDB_API_KEY`：填 [theporndb.net](https://theporndb.net) 的免费 Read Access Token，欧美和 FC2 命中率、封面质量大幅提升
- `JAVTEN_COOKIE` / `JAVTEN_UA`：javten 被 Cloudflare 拦时，浏览器过盾后按脚本内注释复制 Cookie 和 UA 注入（cf_clearance 绑定 IP 和 UA）

## 注意事项

- 请遵守目标网站的访问频率，并发不宜过高
- 仅供个人本地媒体库整理使用
- 整理前重要文件请自行备份

## License

MIT
