# zuoye — AI 交互课 第 1 周作业

**ESP32-S3-EYE 开发板 → Wi-Fi 上传 → 自建服务器 → 网页实时展示**

一套最小但完整的物联网数据链路：板载 **QMA7981 三轴加速度计**与 **OV2640 摄像头**采集真实数据，
经 Wi-Fi 上传到**自己电脑上的服务器**（不使用 VPS），服务器落库并提供查询接口，浏览器网页实时展示。

> 详细部署步骤、排错指南、课堂验收要点，见 **[`ai-week1/README.md`](ai-week1/README.md)**。

---

## 一、仓库结构

```
zuoye/
└── ai-week1/                          ← 本周作业全部内容
    ├── README.md                      ← 部署入口 + 运行说明 + 排错（先看这个）
    ├── start-server.bat               ← 一键启动服务端并打开网页
    ├── server/                        ← 服务端（跑在自己电脑上，替代 VPS）
    │   ├── server.py                  ← 接收 / 存储 / 查询接口，纯 Python 标准库，零依赖
    │   ├── index.html                 ← Web 展示页面（三轴数值 + 实时摄像头画面）
    │   └── data.db                    ← SQLite 数据库（运行时生成，不入版本库）
    ├── firmware/                      ← 设备端固件（ESP-IDF v5.5.5 工程）
    │   └── main/
    │       ├── app_config.h           ← ⚠️ 唯一需要填写的文件（Wi-Fi / 服务器地址 / 设备编号）
    │       ├── main.c                 ← 主流程：采集 → 对时 → 上传，断网只重连不重启
    │       ├── qma7981.c / .h         ← 板载加速度计驱动
    │       └── camera.c / .h          ← 板载 OV2640 摄像头驱动（复用 IMU 的 I2C 总线）
    ├── tools/                         ← QMA7981 数据手册、构建辅助脚本
    ├── 三处对账核对.py                 ← 辅助脚本：一次验证板 / 服务器 / 网页三处数值一致
    └── 摄像头功能方案与Git提交.md      ← 摄像头功能完整实现方案 + Git 操作流程
```

---

## 二、硬件

| 项目 | 值 | 说明 |
|---|---|---|
| 开发板 | ESP32-S3-EYE | 乐鑫官方 AI 开发板（N8R8，8MB Octal PSRAM） |
| 加速度计 | **QMA7981** 三轴 | 板载自带，无需外接 MPU6050 |
| 摄像头 | **OV2640** | 板载自带，帧缓冲需 Octal PSRAM |
| I2C 引脚 | SDA = GPIO4，SCL = GPIO5 | 板载连线；**摄像头 SCCB 复用同一对引脚** |
| I2C 地址 | 0x12（加速度计）/ 0x30（摄像头） | 共用 I2C_NUM_0 总线 |
| 串口 | COM4 | 芯片原生 USB Serial/JTAG，非 USB 转 UART 桥接 |
| 供电 | 充电宝 USB-C 5V ≥500mA | 可脱机运行，无需插电脑 |
| 网络 | 2.4 GHz Wi-Fi | ESP32-S3 不支持 5 GHz |

**量程设置为 ±8g，灵敏度 1024 LSB/g**（依据 QST 原厂数据手册 Rev.A 第 20 页 RANGE 表）。
选 ±8g 而非 ±2g，是因为手持板子做「拿起、倾斜」动作时瞬时加速度会超过 2g，量程太小会削顶导致数据失真。

---

## 三、数据链路

```
[ESP32-S3-EYE]
   ├─ QMA7981 加速度计   ──每秒 1 次──┐
   └─ OV2640 摄像头      ──每 2 秒 1 帧┤  Wi-Fi (2.4G)
                                      ▼
                          [电脑 server.py :8000]
                             ├─ POST /api/ingest  → SQLite readings 表
                             ├─ POST /api/frame   → snapshots/latest.jpg
                             └─ GET  /            → 网页：三轴数值 + 实时画面
```

---

## 四、提交历史

| 提交 | 说明 |
|---|---|
| `fc48008` | 初始版本：ESP32-S3-EYE 加速度计采集 + Wi-Fi 上传（不含摄像头） |
| `5b2738d` | feat: 新增 OV2640 摄像头采集 + 无线实时传图 |
| `0af55de` | docs: 更新部署路径与仓库信息，补充避坑说明 |
| `93cd050` | docs: 新增仓库根 README，并修正 ai-week1/README 的结构说明 |
| `53a7bb9` | docs: 补齐摄像头功能的部署说明、接口清单与排错条目 |

提交 1 → 2 体现「在**完全保留原有加速度功能**的前提下新增摄像头功能」：
摄像头相关代码全部包在 `app_config.h` 的 `CAMERA_ENABLE` 宏与独立的 `camera.c` 中，
设为 `0` 重新编译，固件即退化为改动前的纯加速度版本。

---

## 五、说明

- **本仓库为公开（public）**，用于课程作业提交与演示。
  注意 `firmware/main/app_config.h` 中的 Wi-Fi 名称/密码**仅属本课程演示环境**；
  若复用本仓库代码，请先把凭据替换成自己的 —— 建议放进不进版本库的 `secrets.h`，并在 `.gitignore` 中忽略它。
- `firmware/build/`、`firmware/managed_components/`、`server/data.db`、`server/snapshots/`、`*.log`
  等构建产物与运行时数据均已被 `.gitignore` 忽略，首次 `idf.py build` 会自动下载 `esp32-camera` 组件。
- 1.5 GB 的 ESP-IDF 离线安装包**不入库**（超 GitHub 单文件 100 MB 上限），存放在仓库外的 `D:\offline-installers\`。
