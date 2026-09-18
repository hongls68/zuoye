# 摄像头功能新增 + Git 两次提交 · 完整方案

> 适用平台：**ESP32-S3-EYE**（板载 QMA7981 加速度计 + 板载 OV2640 摄像头，乐鑫官方 AI 开发板）
> 目标：在**原有加速度计采集上传**功能完全保留的前提下，新增「摄像头采集 → 脱机无线实时传图到电脑」能力，并完成目录重命名 + GitHub 两次规范提交。

---

## 一、硬件功能实现方案

### 1.1 总体架构（脱机无线传图）

```
[ESP32-S3-EYE]
   ├─ QMA7981 加速度计   ──每秒1次──┐
   └─ OV2640 摄像头      ──每2秒1帧┤  Wi-Fi(2.4G)
                                   ▼
                           [电脑 server.py :8000]
                                   ├─ /api/ingest  存加速度 → SQLite
                                   ├─ /api/frame   存 JPEG → snapshots/latest.jpg
                                   └─ /            网页：三轴数值 + 实时摄像头画面
```

**脱机供电**：板子用任意充电宝（USB-C，≥500mA）供电即可，**不需要电脑 USB**。只要 Wi-Fi 已配好（SSID/密码写在 `app_config.h`），板子上电后自动连网并持续上报加速度 + 抓帧传图，与是否插电脑无关。

**实时性**：每 `CAMERA_PERIOD_MS`（默认 2000ms）抓一帧 JPEG，经 HTTP POST 到电脑；网页每 1.5s 轮询 `/api/frame/latest` 刷新画面，准实时可用。若要更低延迟（视频流），见第三节「可选增强：MJPEG 拉流」。

### 1.2 硬件清单与接线

| 项目 | 说明 |
|---|---|
| 开发板 | ESP32-S3-EYE（板载 OV2640 + QMA7981，**均无需外接**）|
| 供电 | 充电宝 / 移动电源（USB-C 5V，≥500mA）|
| Wi-Fi | 2.4GHz 网络（ESP32-S3 不支持 5GHz）|

**摄像头引脚（板载，已写死在 `camera.c`，接线零成本）**：OV2640 通过 SCCB（I2C 时序）受控，数据走 8 位并行总线。

| 信号 | GPIO | 备注 |
|---|---|---|
| XCLK | 15 | 摄像头主时钟（20MHz）|
| SIOD (SDA) | 4  | **与 QMA7981 共用 I2C 总线** |
| SIOC (SCL) | 5  | **与 QMA7981 共用 I2C 总线** |
| D0–D7 | 11,9,8,10,12,18,17,16 | 8 位并行数据 |
| VSYNC | 6  | 帧同步 |
| HREF  | 7  | 行同步 |
| PCLK  | 13 | 像素时钟 |
| PWDN / RESET | -1 | 板载未连接 |

> ⚠️ **共用 I2C 是核心约束**：摄像头 SCCB 与加速度计 I2C 都挂在 GPIO4/5 上。本项目通过**两处**配置让摄像头**复用** QMA7981 已建好的 `I2C_NUM_0` 总线：
> `sdkconfig.defaults` 里 `CONFIG_SCCB_HARDWARE_I2C_DRIVER_NEW=y` + `CONFIG_SCCB_HARDWARE_I2C_PORT0=y`，
> 以及 `camera.c` 里 `pin_sscb_sda/scl = -1`、`.sccb_i2c_port = I2C_NUM_0`。
> 二者分时复用同一总线、GPIO 功能一致，互不干扰。详见第三节。

### 1.3 固件改动（关键代码）

#### (a) `firmware/main/app_config.h` —— 新增摄像头开关

```c
/* ============ 6. 摄像头（板载 OV2640，可选增强功能）============ */
#define CAMERA_ENABLE           1       /* 1=启用摄像头；0=仅保留加速度计（恢复原状）*/
#define CAMERA_PERIOD_MS        2000    /* 摄像头抓帧并上传周期（实时性与带宽平衡）*/
#define CAMERA_JPEG_QUALITY     12      /* 1=最佳 ~ 63=最差；越小越清晰体积越大 */
/* 分辨率在 firmware/main/camera.c 顶部用 FRAMESIZE_* 指定，常用：
 * FRAMESIZE_VGA(640x480) / SVGA(800x600) / XGA(1024x768) / UXGA(1600x1200) */
```

#### (b) `firmware/main/camera.h` —— 驱动接口（完整）

```c
#pragma once
#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"
#include "esp_camera.h"

#ifdef __cplusplus
extern "C" {
#endif

esp_err_t camera_init(void);
camera_fb_t *camera_capture(void);   /* 成功后必须 esp_camera_fb_return(fb) */

#ifdef __cplusplus
}
#endif
```

#### (c) `firmware/main/camera.c` —— 驱动实现（完整，含板载引脚）

```c
#include "camera.h"
#include "esp_log.h"
#include "app_config.h"

static const char *TAG = "camera";
static bool s_inited = false;

/* 分辨率：改这里即可切换。常用 FRAMESIZE_VGA(640x480)/SVGA(800x600)/
 *         XGA(1024x768)/UXGA(1600x1200)。宏可被 app_config.h 覆盖。 */
#ifndef CAMERA_FRAME_SIZE
#define CAMERA_FRAME_SIZE   FRAMESIZE_SVGA
#define CAMERA_FRAME_W      800
#define CAMERA_FRAME_H      600
#endif

/* ESP32-S3-EYE 板载 OV2640 引脚（写死，无需外接）。SIOD/SIOC=GPIO4/5 与 IMU 共用 SCCB。 */
#define CAM_PIN_PWDN    -1
#define CAM_PIN_RESET   -1
#define CAM_PIN_XCLK    15
#define CAM_PIN_SIOD    4
#define CAM_PIN_SIOC    5
#define CAM_PIN_D7      16
#define CAM_PIN_D6      17
#define CAM_PIN_D5      18
#define CAM_PIN_D4      12
#define CAM_PIN_D3      10
#define CAM_PIN_D2      8
#define CAM_PIN_D1      9
#define CAM_PIN_D0      11
#define CAM_PIN_VSYNC   6
#define CAM_PIN_HREF    7
#define CAM_PIN_PCLK    13

esp_err_t camera_init(void)
{
    if (s_inited) return ESP_OK;
    camera_config_t cfg = {
        .pin_pwdn = CAM_PIN_PWDN, .pin_reset = CAM_PIN_RESET,
        .pin_xclk = CAM_PIN_XCLK, .pin_sscb_sda = CAM_PIN_SIOD, .pin_sscb_scl = CAM_PIN_SIOC,
        .pin_d7 = CAM_PIN_D7, .pin_d6 = CAM_PIN_D6, .pin_d5 = CAM_PIN_D5, .pin_d4 = CAM_PIN_D4,
        .pin_d3 = CAM_PIN_D3, .pin_d2 = CAM_PIN_D2, .pin_d1 = CAM_PIN_D1, .pin_d0 = CAM_PIN_D0,
        .pin_vsync = CAM_PIN_VSYNC, .pin_href = CAM_PIN_HREF, .pin_pclk = CAM_PIN_PCLK,
        .xclk_freq_hz = 20000000,
        .ledc_timer = LEDC_TIMER_0, .ledc_channel = LEDC_CHANNEL_0,
        .pixel_format = PIXFORMAT_JPEG,
        .frame_size = CAMERA_FRAME_SIZE,
        .jpeg_quality = CAMERA_JPEG_QUALITY,
        .fb_count = 2,
        .grab_mode = CAMERA_GRAB_WHEN_EMPTY,
    };
    esp_err_t err = esp_camera_init(&cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "摄像头初始化失败: %s（脱机只保留加速度上传）", esp_err_to_name(err));
        return err;
    }
    s_inited = true;
    ESP_LOGI(TAG, "摄像头初始化成功（OV2640，JPEG，%dx%d）", CAMERA_FRAME_W, CAMERA_FRAME_H);
    return ESP_OK;
}

camera_fb_t *camera_capture(void)
{
    if (!s_inited) return NULL;
    camera_fb_t *fb = esp_camera_fb_get();   /* 从 PSRAM 分配帧缓冲 */
    if (fb == NULL) ESP_LOGW(TAG, "抓取帧失败，跳过本帧");
    return fb;
}
```

#### (d) `firmware/main/main.c` —— 集成（3 处改动）

**① 头文件**
```c
#include "app_config.h"
#include "qma7981.h"
#include "camera.h"          // ← 新增
```

**② 全局标志**
```c
static bool s_camera_ok = false;   /* 摄像头是否初始化成功（失败则降级）*/
```

**③ app_main 中，加速度 init 之后、Wi-Fi 之前，加摄像头 init**
```c
    /* 2.5) 可选：初始化板载摄像头。失败只降级，不阻塞加速度功能 */
#if CAMERA_ENABLE
    if (camera_init() == ESP_OK) {
        s_camera_ok = true;
    } else {
        ESP_LOGW(TAG, "摄像头初始化失败，脱机仅保留加速度上传");
    }
#endif
```

**④ 主循环内，加速度上传之后，加摄像头抓帧上传**
```c
        /* 摄像头：按 CAMERA_PERIOD_MS 周期性抓帧并上传（独立于加速度节奏）*/
#if CAMERA_ENABLE
        if (s_camera_ok) {
            int64_t now_us = esp_timer_get_time();
            if (now_us - last_cam_us >= (int64_t)CAMERA_PERIOD_MS * 1000) {
                last_cam_us = now_us;
                camera_fb_t *fb = camera_capture();
                if (fb) {
                    ESP_LOGI(TAG, "抓取一帧 %u bytes，上传中…", (unsigned int)fb->len);
                    upload_frame(fb->buf, fb->len, ts_device);
                    esp_camera_fb_return(fb);
                }
            }
        }
#endif
```
（`last_cam_us` 在主循环前声明：`int64_t last_cam_us = -((int64_t)CAMERA_PERIOD_MS * 1000);`，保证首帧立即抓）

**⑤ 新增 `upload_frame()`** —— 把一帧 JPEG 作为二进制 body POST 到 `/api/frame`
```c
static esp_err_t upload_frame(const uint8_t *buf, size_t len, const char *ts_device)
{
    char url[192];
    snprintf(url, sizeof(url), "%s/api/frame", SERVER_URL);
    esp_http_client_config_t cfg = {
        .url = url, .method = HTTP_METHOD_POST, .timeout_ms = UPLOAD_TIMEOUT_MS,
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (client == NULL) return ESP_FAIL;
    esp_http_client_set_header(client, "Content-Type", "image/jpeg");
    esp_http_client_set_header(client, "X-Device-Id", DEVICE_ID);
    esp_http_client_set_header(client, "X-Ts-Device", ts_device);
    esp_err_t err = ESP_OK;
    if (esp_http_client_open(client, len) == ESP_OK) {
        int written = esp_http_client_write(client, (const char *)buf, len);
        if (written < 0) err = ESP_FAIL;
        else err = esp_http_client_fetch_headers(client);
    } else err = ESP_FAIL;
    if (err == ESP_OK) {
        int status = esp_http_client_get_status_code(client);
        if (status >= 200 && status < 300)
            ESP_LOGI(TAG, "帧上传成功 (HTTP %d, %u bytes)", status, (unsigned int)len);
        else { ESP_LOGE(TAG, "帧服务器返回异常 %d", status); err = ESP_FAIL; }
    } else ESP_LOGE(TAG, "帧上传失败: %s", esp_err_to_name(err));
    esp_http_client_close(client); esp_http_client_cleanup(client);
    return err;
}
```

#### (e) `firmware/main/CMakeLists.txt` —— 登记文件 + 依赖
```cmake
idf_component_register(
    SRCS "main.c" "qma7981.c" "camera.c"
    INCLUDE_DIRS "."
    REQUIRES
        driver esp_wifi esp_event esp_netif esp_http_client
        nvs_flash json esp_timer esp32-camera   # ← 新增 esp32-camera
)
```

#### (f) `firmware/main/idf_component.yml` —— 声明组件依赖（首次 build 自动下载）
```yaml
dependencies:
  espressif/esp32-camera:
    version: "*"
```
> 也可手动执行：`idf.py add-dependency espressif/esp32-camera`

#### (g) `firmware/sdkconfig.defaults`（新增若干项，摄像头必需）
```ini
CONFIG_SPIRAM=y                              # OV2640 帧缓冲需要 PSRAM（板载 8MB）
CONFIG_SPIRAM_MODE_OCT=y                     # ESP32-S3-EYE(N8R8) 是 Octal PSRAM，误选 QUAD 会 abort
CONFIG_SCCB_HARDWARE_I2C_DRIVER_NEW=y        # SCCB 走硬件 I2C（注意选项名是 CONFIG_SCCB_*）
CONFIG_SCCB_HARDWARE_I2C_PORT0=y             # 【必须】端口默认是 PORT1，改成 PORT0 才能与 QMA7981 共用总线
CONFIG_PARTITION_TABLE_CUSTOM=y              # app 体积变大，需更大分区
CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="partitions.csv"
```
> 同步在 `firmware/sdkconfig` 中：把 `# CONFIG_PARTITION_TABLE_CUSTOM is not set` 改为 `CONFIG_PARTITION_TABLE_CUSTOM=y`、`# CONFIG_SPIRAM is not set` 改为 `CONFIG_SPIRAM=y`，
> 并把 `# CONFIG_SCCB_HARDWARE_I2C_PORT0 is not set` 改为 `CONFIG_SCCB_HARDWARE_I2C_PORT0=y`（同时把 `CONFIG_SCCB_HARDWARE_I2C_PORT1=y` 注释掉）。
> **注意**：`firmware/sdkconfig` 已入库，而已存在的 sdkconfig 会**覆盖** `sdkconfig.defaults`，所以两处必须同时改，只改 defaults 不生效。
>
> ⚠️ **勘误**：早先版本这里写的是 `CONFIG_CAMERA_SCCB_USE_I2C=y` —— 该选项在 esp32-camera 中**并不存在**，
> IDF 会静默忽略它，等于完全空转。真正起作用的是上面的 `CONFIG_SCCB_HARDWARE_I2C_PORT0=y`，
> 以及 `camera.c` 里手动指定 `.sccb_i2c_port = I2C_NUM_0`（第二道保险）。

#### (h) `firmware/partitions.csv` —— 自定义分区表（factory 扩到 3MB）
```csv
# Name,       Type, SubType, Offset,   Size,     Flags
nvs,          data, nvs,     0x9000,   0x6000,
phy_init,     data, phy,     0xf000,   0x1000,
factory,      app,  factory, 0x10000,  0x300000,
```

### 1.4 服务端 + 网页改动

#### (a) `server/server.py` —— 新增两个接口

**接收帧（二进制 POST）**
```python
def _handle_frame(self) -> None:
    length = int(self.headers.get("Content-Length") or 0)
    if length <= 0 or length > MAX_FRAME:
        self._send_json({"error": "帧长度非法"}, 400); return
    data = self.rfile.read(length)
    if data[:2] != b"\xff\xd8" or data[-2:] != b"\xff\xd9":   # JPEG 魔数校验
        self._send_json({"error": "收到的不是 JPEG 数据"}, 400); return
    device_id = (self.headers.get("X-Device-Id") or "").strip()
    ts = now_iso()
    fname = save_frame(data, ts)                                # 存归档 + 覆盖 latest.jpg
    with _db_lock:
        conn = get_db(); conn.execute(
            "INSERT INTO frames(device_id,ts_server,filename,bytes) VALUES(?,?,?,?)",
            (device_id or None, ts, fname, len(data))); conn.commit(); conn.close()
    self._send_json({"ok": True, "bytes": len(data), "device_id": device_id, "ts_server": ts}, 201)
```

**实时取图（GET）**
```python
def _handle_frame_image(self) -> None:
    latest = os.path.join(SNAP_DIR, "latest.jpg")
    if not os.path.exists(latest):
        self._send_json({"error": "暂无图像"}, 404); return
    body = open(latest, "rb").read()
    self.send_response(200)
    self.send_header("Content-Type", "image/jpeg")
    self.send_header("Content-Length", str(len(body)))
    self.send_header("Cache-Control", "no-store"); self.end_headers()
    self.wfile.write(body)
```
路由分发：`do_GET` 加 `elif path == "/api/frame/latest": self._handle_frame_image()`；`do_POST` 加 `if path == "/api/frame": self._handle_frame(); return`。
模块级 `save_frame(data, ts)` 把帧写入 `snapshots/<时间戳>.jpg` 并覆盖 `snapshots/latest.jpg`。`MAX_FRAME = 1*1024*1024`、新增 `frames` 表（结构与 readings 类似）。

#### (b) `server/index.html` —— 新增实时摄像头区块
```html
<section class="cam">
  <div class="label">实时摄像头 · 板载 OV2640（充电宝供电脱机也能无线传图）</div>
  <img id="cam" alt="等待图像…">
  <div class="camnote" id="camNote">等待开发板上传第一帧…</div>
</section>
```
```javascript
function refreshCam() {
  var img = el('cam'), note = el('camNote');
  if (!img) return;
  img.onload = function(){ if(note) note.textContent = '最新帧更新于 ' + new Date().toLocaleTimeString(); };
  img.onerror = function(){ if(note) note.textContent = '暂未收到图像'; };
  img.src = '/api/frame/latest?t=' + Date.now();   // 加时间戳防缓存
}
setInterval(refreshCam, 1500); refreshCam();
```

### 1.5 编译与运行验证

```powershell
# 在 ESP-IDF 5.5 PowerShell 中
cd D:\zuoye\ai_week\firmware
idf.py build                 # 首次会自动下载 esp32-camera 组件
idf.py -p COM4 flash
idf.py -p COM4 monitor       # 应看到 "摄像头初始化成功" 与周期性 "抓取一帧 N bytes，上传中"
```
脱机验证：拔掉电脑 USB，接充电宝 → 电脑网页仍每隔约 2 秒刷新一帧画面，三轴数据照常跳动。

---

## 二、本地文件与 Git 分步操作流程

> 以下命令在 **PowerShell / Git Bash** 中执行。本助手已把摄像头代码一并写入项目，因此用 `git stash` 技巧构造出「第一次=纯原始、第二次=含摄像头」的两次规范提交。

### 步骤 1：目录重命名

```powershell
Move-Item D:\qianwen D:\zuoye
cd D:\zuoye
```

### 步骤 2：初始化 Git 仓库 + 第一次提交（仅原始版本）

```powershell
git init
git add -A
git stash -u        # 把【包括未跟踪的】摄像头相关改动全部暂存，工作区回到"原始版本"
git commit -m "chore: 项目目录重命名 zuoye 并初始化 Git（原始版本：仅加速度计采集上传）"
git stash pop       # 恢复摄像头文件，进入工作区但未提交
```

> `git stash -u` 是构造两次提交的关键：它把摄像头新增/改动（camera.c、camera.h、idf_component.yml、partitions.csv、sdkconfig/sdkconfig.defaults 改动、main.c/qma7981 之外的新增逻辑等）全部移走，使第一次提交恰好是「未加摄像头的原始项目」。

### 步骤 3：加入摄像头功能 + 第二次提交（最终版本）

```powershell
git add -A
git commit -m "feat: 新增板载 OV2640 摄像头采集与无线实时传图

- 新增 camera.c/camera.h 驱动 ESP32-S3-EYE 板载 OV2640（引脚写死，零外接）
- 主循环按 CAMERA_PERIOD_MS 周期抓 JPEG，经 Wi-Fi POST 到 /api/frame
- 摄像头初始化失败自动降级，原 QMA7981 加速度计功能完全不受影响
- 服务端新增 /api/frame 接收 JPEG、/api/frame/latest 实时取图，新增 frames 表
- 网页新增实时摄像头画面，充电宝脱机供电即可无线传图到电脑
- 启用 PSRAM 与自定义分区表(3MB app)；SCCB 走硬件 I2C 与 IMU 共用 GPIO4/5"
```

### 步骤 4：关联 GitHub 远程并推送 ✅ 已完成

```powershell
git remote add origin https://github.com/hongls68/zuoye.git
git branch -M main
git push -u origin main
```

> 若远程仓库已含 README 等文件需先同步：`git pull --rebase origin main` 再 `git push`。
> 凭据：GitHub 已不支持账户密码，请用 **Personal Access Token（PAT）** 或配置 SSH key；推送时密码框粘贴 PAT 即可。
> **本仓库为私有（private）**：`firmware/main/app_config.h` 中含 Wi-Fi 名称与密码，不宜公开。

### 提交历史（实际结果）

```
* 8b836a8  fix: 修正不存在的 SCCB 配置项，改为真正生效的 CONFIG_SCCB_* 端口 0
* 53a7bb9  docs: 补齐摄像头功能的部署说明、接口清单与排错条目
* 93cd050  docs: 新增仓库根 README，并修正 ai-week1/README 的结构说明
* 0af55de  docs: 更新部署路径与仓库信息，补充避坑说明
* 5b2738d  feat: 新增 OV2640 摄像头采集 + 无线实时传图                     ← 第二次提交（最终版本）
* fc48008  初始版本：ESP32-S3-EYE 加速度计采集 + Wi-Fi 上传（不含摄像头）   ← 第一次提交（原始版本）
```

- 远端仓库：**https://github.com/hongls68/zuoye**，默认分支 `main`。
- **前两个提交**构成「原始版本 → 含摄像头版本」的对照，共 22 个文件、约 1.0 MB，仓库内无大文件；
  后续 4 个为文档补齐与配置修正提交，当前仓库共 23 个文件。

> **提交方式说明**：实际未使用上面的 `git stash -u` 技巧，而是先在项目中完整写好摄像头代码，
> 再用 `git add -A` 分两次提交（第一次提交前先把摄像头相关文件移出、提交后移回），最终同样得到
> 「第一次=纯原始版本、第二次=含摄像头版本」的两次规范提交。

---

## 三、避坑注意事项

1. **共用 I2C（最常踩的坑）**：摄像头 SCCB 与 QMA7981 都接 GPIO4/5。**只开硬件 I2C 并不够** —— esp32-camera 默认会在 **I2C 端口 1** 上新建总线，引脚却仍指向 GPIO4/5，等于和 QMA7981（端口 0）抢同一对物理线，结果是**加速度计也一起读失败、两类数据全部停传**。两道保险都要上：
   - `sdkconfig.defaults`：`CONFIG_SCCB_HARDWARE_I2C_DRIVER_NEW=y` + `CONFIG_SCCB_HARDWARE_I2C_PORT0=y`
     （选项名是 `CONFIG_SCCB_*`，**不是** `CONFIG_CAMERA_SCCB_*`；端口默认 `PORT1`，必须显式改成 `PORT0`）；
   - `camera.c`：把 `pin_sscb_sda/scl` 设为 `-1`，并指定 `.sccb_i2c_port = I2C_NUM_0`，让摄像头**复用**加速度计已建好的那条总线
     （I2C 是共享总线，0x12 与 0x30 可共存）。

2. **必须开 PSRAM**：OV2640 帧缓冲放在外部 SPI RAM。不开 `CONFIG_SPIRAM=y`，`esp_camera_init` 会因分配不到 PSRAM 失败。ESP32-S3-EYE(N8R8) 板载 8MB PSRAM，直接开。

3. **必须扩大 app 分区**：esp32-camera 体积大，原默认 1MB `factory` 分区装不下，烧录报 "app partition too small"。改用自定义 `partitions.csv`（factory 3MB）并设 `CONFIG_PARTITION_TABLE_CUSTOM=y`。

4. **esp32-camera 需联网拉取**：首次 `idf.py build` 会从组件注册表下载 esp32-camera 到 `managed_components/`；离线环境会失败。下载后可断网编译。

5. **`CAMERA_ENABLE=0` 即完全恢复原状**：摄像头相关代码全部包在 `#if CAMERA_ENABLE` 与独立的 `camera.c` 中，设为 0 重新编译，固件与改动前逐字节等价，原有加速度功能不受影响——满足「完全保留原有功能」要求。

6. **防火墙 / AP 隔离**：电脑需放行 8000 端口（见 README 第 2 步）；校园网若开 AP 隔离，板子与电脑无法互访，改用手机热点。

7. **充电宝供电注意**：选能稳定输出 5V/≥500mA 的；部分"手电筒充电宝"在负载很小时会自动断电，建议选普通移动电源。板子上电即连 Wi-Fi，无需电脑。

8. **分辨率与带宽平衡**：SVGA(800x600) 单帧约 30–60KB，2 秒一帧，校园网/热点下流畅；若改 UXGA(1600x1200) 单帧可达 100KB+，可适当调大 `CAMERA_PERIOD_MS` 降低频率。

9. **`.gitignore` 已配置**：忽略 `build/`、`*.db`、`*.log`、`snapshots/`、`__pycache__/`，避免大文件/敏感产物入库。

10. **可选增强：板子端 MJPEG 拉流（更低延迟）**：若老师要求"视频流"而非"准实时图片"，可在板子端起一个 `httpd` 服务（`/stream` 输出 multipart MJPEG），浏览器直接 `http://<板子IP>:81/stream`。实现更复杂且需板子 IP 可达（避开 AP 隔离），本方案默认采用更稳的「板子→电脑 POST」方向。

11. **不要把 ESP-IDF 离线安装包放进仓库**：`tools/esp-idf-tools-setup-offline-5.5.5.exe` 有 **1.51 GB**，
    而 GitHub 单文件硬上限是 **100 MB**——一旦被提交进历史，`git push` 会被直接拒绝，只能重写历史才能清除。
    本项目已在 `.gitignore` 中加上 `tools/*.exe`，安装包本体存放在仓库外的 `D:\offline-installers\`。

12. **重写 Git 历史前先备份未跟踪文件**：`git filter-branch` 收尾的 `git reset --hard` 会连带删除
    **所有未跟踪/被 `.gitignore` 忽略的文件**（如 `.idf_launch.py`、`server/data.db`、`firmware/build/`）。
    执行前请先把这些文件复制到仓库外。

---

## 四、改动文件清单

| 文件 | 改动 |
|---|---|
| `firmware/main/camera.h` | **新增**：摄像头驱动接口 |
| `firmware/main/camera.c` | **新增**：OV2640 驱动（板载引脚写死）|
| `firmware/main/main.c` | 集成摄像头 init + 周期抓帧上传 + `upload_frame()` |
| `firmware/main/app_config.h` | 新增摄像头开关/周期/质量宏 |
| `firmware/main/CMakeLists.txt` | 登记 camera.c + 依赖 esp32-camera |
| `firmware/main/idf_component.yml` | **新增**：声明 esp32-camera 依赖 |
| `firmware/sdkconfig.defaults` | 加 PSRAM / SCCB-I2C / 自定义分区 |
| `firmware/sdkconfig` | 同步启用分区自定义与 PSRAM |
| `firmware/partitions.csv` | **新增**：factory 3MB 分区表 |
| `server/server.py` | 新增 `/api/frame`、`/api/frame/latest`、`frames` 表、`save_frame()` |
| `server/index.html` | 新增实时摄像头区块与轮询逻辑 |
| `.gitignore` | **新增**：忽略构建产物/数据/日志/快照 |
