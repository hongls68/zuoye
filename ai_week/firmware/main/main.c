/*
 * main.c —— AI交互课 第1周 设备端固件
 *
 * 职责：
 *   1) 读取 ESP32-S3-EYE 板载 QMA7981 三轴加速度计（真实测量值）
 *   2) 通过 Wi-Fi 把「数值 + 单位 + 时间戳 + 设备编号」POST 到自建服务器
 *   3) 每秒一次；网络异常时只重连、不重启，不产生假数据
 *
 * 配置：所有需要修改的内容集中在 app_config.h（Wi-Fi、服务器地址、设备编号）
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <sys/time.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"

#include "esp_err.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"
#include "esp_sntp.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "nvs_flash.h"

#include "esp_http_client.h"
#include "cJSON.h"

#include "app_config.h"
#include "qma7981.h"
#include "camera.h"

static const char *TAG = "app";

#define WIFI_CONNECTED_BIT BIT0
#define WIFI_FAIL_BIT      BIT1

static EventGroupHandle_t s_wifi_evt;
static volatile bool s_wifi_connected = false;
static volatile bool s_time_synced = false;
static bool s_camera_ok = false;   /* 摄像头是否初始化成功（失败则降级）*/

/* ---------------- Wi-Fi ---------------- */

/* Wi-Fi 重连节流：避免断开瞬间高频调用 esp_wifi_connect() 刷日志 */
static int64_t s_last_reconnect_us = 0;

static void wifi_event_handler(void *arg, esp_event_base_t base,
                               int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        wifi_event_sta_disconnected_t *disc =
            (wifi_event_sta_disconnected_t *)data;
        s_wifi_connected = false;
        xEventGroupClearBits(s_wifi_evt, WIFI_CONNECTED_BIT);
        xEventGroupSetBits(s_wifi_evt, WIFI_FAIL_BIT);

        ESP_LOGW(TAG, "Wi-Fi 已断开（原因码 %d），正在重新连接…（不重启设备）",
                 disc->reason);

        /*
         * 【关键】ESP-IDF 的 Wi-Fi 驱动默认不会自动重连，必须在断连事件里
         * 显式调用 esp_wifi_connect()，否则网络恢复后板子永远连不回来。
         * 这里做 1 秒节流，防止密码错误等场景下高频重试刷爆日志。
         */
        int64_t now = esp_timer_get_time();
        if (now - s_last_reconnect_us > 1000000) {
            s_last_reconnect_us = now;
            esp_wifi_connect();
        }
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *evt = (ip_event_got_ip_t *)data;
        s_wifi_connected = true;
        ESP_LOGI(TAG, "Wi-Fi 已连接，本机 IP: " IPSTR, IP2STR(&evt->ip_info.ip));
        xEventGroupClearBits(s_wifi_evt, WIFI_FAIL_BIT);
        xEventGroupSetBits(s_wifi_evt, WIFI_CONNECTED_BIT);
    }
}

static esp_err_t wifi_init_sta(void)
{
    s_wifi_evt = xEventGroupCreate();

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL, NULL));

    wifi_config_t wifi_cfg = {0};
    strncpy((char *)wifi_cfg.sta.ssid, WIFI_SSID, sizeof(wifi_cfg.sta.ssid) - 1);
    strncpy((char *)wifi_cfg.sta.password, WIFI_PASSWORD,
            sizeof(wifi_cfg.sta.password) - 1);
    /*
     * 认证阈值：设为 WPA2_PSK 表示接受 WPA2 及以上的加密方式，
     * 覆盖绝大多数家庭路由器与校园网。若你的 Wi-Fi 是 WPA3 或企业级认证(802.1X)，
     * 需要相应改成 WIFI_AUTH_WPA3_PSK 或走 EAP 配置，本固件未实现企业认证。
     *
     * 注意：ESP-IDF 的 Wi-Fi 驱动自身会在断开后自动重连，
     * 下面的断连事件处理只是同步状态标志并打印日志，不负责重连动作本身。
     */
    wifi_cfg.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
    wifi_cfg.sta.pmf_cfg.capable = true;
    wifi_cfg.sta.pmf_cfg.required = false;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg));
    ESP_ERROR_CHECK(esp_wifi_start());

    /*
     * 【关键】关闭 Wi-Fi 省电（Modem-sleep），必须在 esp_wifi_start() 之后调用。
     *
     * ESP-IDF 默认让 STA 连上后进入 WIFI_PS_MIN_MODEM（省电最小模式），
     * 串口会打印 `wifi:pm start, type: 1`。此时板子按「监听间隔」周期性休眠，
     * 入站帧由 AP 缓存、等到 DTIM 才下发；某些 AP 还会把监听间隔放大
     * （实测某校园 AP 从 102ms 放大到 307ms，日志：scale listen interval ... 307200 us）。
     *
     * 后果就是本工程踩到的坑：板子发完 SYN 立刻休眠，SYN-ACK 被 AP 压着不往下发，
     * 表现为「Wi-Fi 显示已连接、RSSI 高达 -36 dBm，但 HTTP 一直连不上」：
     *     E esp-tls: [sock=54] select() timeout
     *     E transport_base: Failed to open a new connection: 32774
     *     E HTTP_CLIENT: Connection failed, sock < 0
     * 甚至偶发连上了但 POST 的数据帧发不出去，服务器收不到完整请求：
     *     W HTTP_CLIENT: Connection timed out before data was ready!
     *
     * 关掉省电后射频常开、即收即回，代价是功耗略升。
     * 本项目由 USB 供电，功耗不是约束，因此始终关闭。
     */
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));

    ESP_LOGI(TAG, "正在连接 Wi-Fi: %s", WIFI_SSID);
    return ESP_OK;
}

/* 等待 Wi-Fi 就绪；已连接则立即返回 true */
static bool wifi_wait_connected(uint32_t timeout_ms)
{
    if (s_wifi_connected) {
        return true;
    }
    EventBits_t bits = xEventGroupWaitBits(
        s_wifi_evt, WIFI_CONNECTED_BIT, pdFALSE, pdFALSE,
        pdMS_TO_TICKS(timeout_ms));
    return (bits & WIFI_CONNECTED_BIT) != 0;
}

/* ---------------- 时间同步 ----------------
 *
 * ESP32 没有掉电保持的 RTC，上电后系统时间从 1970 年开始。
 * 为了让「板端采集时间」是真实可读的时间戳，这里用 SNTP 对时（东八区）。
 * 若对时失败，不会伪造时间，而是退化为「上电后经过的秒数」并明确标注未同步。
 */
/* 启动 SNTP 对时。
 * 说明：这里刻意不使用 esp_sntp_config_t 的 sync_cb 回调字段——该字段在部分
 * IDF 配置下受宏保护，直接赋值可能编译失败。对时是否成功统一由
 * esp_netif_sntp_sync_wait() 的返回值判定。
 */
static void time_sync_start(void)
{
    /* 设为东八区，与服务器入库时间口径一致，便于三处对账 */
    setenv("TZ", "CST-8", 1);
    tzset();

    /*
     * 注意：ESP-IDF v5.x 中该宏名为 ESP_NETIF_SNTP_DEFAULT_CONFIG，
     * 旧版（v4.x）的 ESP_SNTP_CONFIG_DEFAULT 在 v5.5 已不存在，
     * 用它会得到 "implicit declaration of function" 编译错误。
     */
    esp_sntp_config_t cfg = ESP_NETIF_SNTP_DEFAULT_CONFIG("ntp.aliyun.com");
    esp_netif_sntp_init(&cfg);
    ESP_LOGI(TAG, "已启动 SNTP，对时服务器: ntp.aliyun.com");
}

static bool time_sync_wait(uint32_t timeout_ms)
{
    if (esp_sntp_get_sync_status() == SNTP_SYNC_STATUS_COMPLETED) {
        s_time_synced = true;
        return true;
    }
    ESP_LOGI(TAG, "等待 SNTP 对时（最多 %u 秒）…", timeout_ms / 1000);
    if (esp_netif_sntp_sync_wait(pdMS_TO_TICKS(timeout_ms)) == ESP_OK) {
        s_time_synced = true;
        return true;
    }
    ESP_LOGW(TAG, "SNTP 对时失败，ts_device 将退化为上电秒数并标注未同步");
    return false;
}

/* 生成板端时间戳字符串。已对时 -> ISO8601(+08:00)；未对时 -> 诚实标注 */
static void make_device_timestamp(char *buf, size_t len)
{
    if (s_time_synced) {
        struct timeval tv;
        gettimeofday(&tv, NULL);
        struct tm tm_info;
        localtime_r(&tv.tv_sec, &tm_info);
        char base[32];
        strftime(base, sizeof(base), "%Y-%m-%dT%H:%M:%S", &tm_info);
        snprintf(buf, len, "%s.%03ld+08:00", base, tv.tv_usec / 1000);
    } else {
        /* 未对时时绝不编造日期，用运行时长表达 */
        snprintf(buf, len, "uptime+%.3fs(time_not_synced)",
                 esp_timer_get_time() / 1000000.0);
    }
}

/* ---------------- 上传 ---------------- */

static esp_err_t upload_reading(const qma7981_sample_t *s,
                                const char *ts_device)
{
    /* 组装 JSON：数值、单位、时间戳、设备编号 */
    cJSON *root = cJSON_CreateObject();
    if (root == NULL) {
        return ESP_ERR_NO_MEM;
    }
    cJSON_AddStringToObject(root, "device_id", DEVICE_ID);
    cJSON_AddStringToObject(root, "sensor", "qma7981_accel");
    cJSON_AddStringToObject(root, "unit", "g");
    cJSON_AddNumberToObject(root, "ax", s->ax);
    cJSON_AddNumberToObject(root, "ay", s->ay);
    cJSON_AddNumberToObject(root, "az", s->az);
    cJSON_AddNumberToObject(root, "ax_raw", s->x_raw);
    cJSON_AddNumberToObject(root, "ay_raw", s->y_raw);
    cJSON_AddNumberToObject(root, "az_raw", s->z_raw);
    cJSON_AddBoolToObject(root, "is_new_sample", s->is_new);
    cJSON_AddBoolToObject(root, "time_synced", s_time_synced);
    cJSON_AddStringToObject(root, "ts_device", ts_device);

    char *payload = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (payload == NULL) {
        return ESP_ERR_NO_MEM;
    }

    char url[192];
    snprintf(url, sizeof(url), "%s/api/ingest", SERVER_URL);

    esp_http_client_config_t cfg = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = UPLOAD_TIMEOUT_MS,
        .disable_auto_redirect = false,
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (client == NULL) {
        free(payload);
        return ESP_FAIL;
    }

    esp_http_client_set_header(client, "Content-Type", "application/json");
    esp_err_t err = esp_http_client_open(client, strlen(payload));
    if (err == ESP_OK) {
        int written = esp_http_client_write(client, payload, strlen(payload));
        if (written < 0) {
            err = ESP_FAIL;
        } else {
            err = esp_http_client_fetch_headers(client);
        }
    }

    if (err == ESP_OK) {
        int status = esp_http_client_get_status_code(client);
        if (status >= 200 && status < 300) {
            ESP_LOGI(TAG, "上传成功 (HTTP %d): ax=%.3f ay=%.3f az=%.3f g",
                     status, s->ax, s->ay, s->az);
        } else {
            ESP_LOGE(TAG, "服务器返回异常状态码 %d", status);
            err = ESP_FAIL;
        }
    } else {
        ESP_LOGE(TAG, "上传失败: %s（检查服务器是否运行、IP 是否填对、防火墙是否放行）",
                 esp_err_to_name(err));
    }

    esp_http_client_close(client);
    esp_http_client_cleanup(client);
    free(payload);
    return err;
}

/* ---------------- 摄像头帧上传 ----------------
 *
 * 把一帧 JPEG 作为二进制 body POST 到服务器 /api/frame（沿用加速度上报的 HTTP 客户端）。
 * 用自定义请求头携带设备编号与板端时间，便于服务器落库与展示。
 * 失败不致命：下一帧会重试，不阻塞加速度上传。
 */
static esp_err_t upload_frame(const uint8_t *buf, size_t len,
                              const char *ts_device)
{
    char url[192];
    snprintf(url, sizeof(url), "%s/api/frame", SERVER_URL);

    esp_http_client_config_t cfg = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = UPLOAD_TIMEOUT_MS,
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (client == NULL) {
        return ESP_FAIL;
    }

    esp_http_client_set_header(client, "Content-Type", "image/jpeg");
    esp_http_client_set_header(client, "X-Device-Id", DEVICE_ID);
    esp_http_client_set_header(client, "X-Ts-Device", ts_device);

    /*
     * 【坑】这里必须原样保留 esp_http_client_open() 返回的错误码。
     * 早期版本写成 `if (open(...) == ESP_OK) {...} else { err = ESP_FAIL; }`，
     * 把 ESP_ERR_HTTP_CONNECT / ESP_ERR_HTTP_READ_TIMEOUT 等真实原因统一压成
     * ESP_FAIL，串口只剩一句「帧上传失败: ESP_FAIL」，排查时等于自断线索。
     */
    esp_err_t err = esp_http_client_open(client, len);
    if (err == ESP_OK) {
        int written = esp_http_client_write(client, (const char *)buf, len);
        if (written < 0) {
            err = ESP_FAIL;
        } else {
            err = esp_http_client_fetch_headers(client);
        }
    }

    if (err == ESP_OK) {
        int status = esp_http_client_get_status_code(client);
        if (status >= 200 && status < 300) {
            ESP_LOGI(TAG, "帧上传成功 (HTTP %d, %u bytes)", status,
                     (unsigned int)len);
        } else {
            ESP_LOGE(TAG, "帧服务器返回异常状态码 %d", status);
            err = ESP_FAIL;
        }
    } else {
        ESP_LOGE(TAG, "帧上传失败: %s", esp_err_to_name(err));
    }

    esp_http_client_close(client);
    esp_http_client_cleanup(client);
    return err;
}

/* ---------------- 主流程 ---------------- */

void app_main(void)
{
    ESP_LOGI(TAG, "===== ESP32-S3-EYE 传感器上传固件启动 =====");
    ESP_LOGI(TAG, "设备编号: %s", DEVICE_ID);
    ESP_LOGI(TAG, "目标服务器: %s", SERVER_URL);

    /* 0) 配置检查：避免用户忘记改占位符就烧录，导致连不上还以为板子坏了 */
    if (strstr(WIFI_SSID, "在这里填") != NULL ||
        strstr(SERVER_URL, "192.168.1.100") != NULL ||
        strstr(DEVICE_ID, "group00") != NULL) {
        ESP_LOGE(TAG, "检测到 app_config.h 尚未填写完整！");
        ESP_LOGE(TAG, "请先编辑 main/app_config.h 填入 Wi-Fi、服务器地址、设备编号，再重新编译烧录。");
        ESP_LOGE(TAG, "固件将持续重试，但不会上传任何数据（避免产生假数据）。");
    }

    /* 1) NVS —— Wi-Fi 驱动依赖 */
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES ||
        ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    /* 2) 初始化板载加速度计。失败时不上传任何数据，只报错。 */
    uint8_t chip_id = 0;
    ret = qma7981_init(&chip_id);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "加速度计初始化失败（%s）。不上传数据，避免伪造读数。",
                 esp_err_to_name(ret));
        while (true) {
            ESP_LOGE(TAG, "传感器不可用，停止上传。请检查硬件后复位。");
            vTaskDelay(pdMS_TO_TICKS(10000));
        }
    }

    /* 2.5) 可选：初始化板载摄像头。失败只降级，不阻塞加速度功能 */
#if CAMERA_ENABLE
    if (camera_init() == ESP_OK) {
        s_camera_ok = true;
    } else {
        ESP_LOGW(TAG, "摄像头初始化失败，脱机仅保留加速度上传");
    }
#endif

    /* 3) 连接 Wi-Fi */
    ESP_ERROR_CHECK(wifi_init_sta());
    if (!wifi_wait_connected(20000)) {
        ESP_LOGW(TAG, "首次连接超时，将在后台继续重试");
    }

    /* 4) 对时 */
    if (wifi_wait_connected(5000)) {
        time_sync_start();
        time_sync_wait(10000);
    }

    /* 5) 打印 MAC，用于核对「数据确实来自本组设备」 */
    uint8_t mac[6] = {0};
    if (esp_read_mac(mac, ESP_MAC_WIFI_STA) == ESP_OK) {
        ESP_LOGI(TAG, "本机 Wi-Fi MAC: %02X:%02X:%02X:%02X:%02X:%02X",
                 mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    }

    ESP_LOGI(TAG, "开始循环采集并上传，周期 %d ms", SAMPLE_PERIOD_MS);

    /* 6) 主循环 */
    uint32_t fail_streak = 0;
#if CAMERA_ENABLE
    int64_t last_cam_us = -((int64_t)CAMERA_PERIOD_MS * 1000);  /* 首帧立即抓 */
#endif
    while (true) {
        int64_t t0 = esp_timer_get_time();

        qma7981_sample_t s;
        ret = qma7981_read(&s);
        if (ret != ESP_OK) {
            ESP_LOGE(TAG, "读取传感器失败: %s，本次不上传（绝不编造数值）",
                     esp_err_to_name(ret));
            vTaskDelay(pdMS_TO_TICKS(SAMPLE_PERIOD_MS));
            continue;
        }

        /* 串口打印原始值 + 换算值，便于与服务器、网页三处对账 */
        ESP_LOGI(TAG, "raw: x=%6d y=%6d z=%6d | g: ax=%+.3f ay=%+.3f az=%+.3f%s",
                 s.x_raw, s.y_raw, s.z_raw, s.ax, s.ay, s.az,
                 s.is_new ? "" : " (NEWDATA=0)");

        /* Wi-Fi 未就绪时只重连，不重启、不缓存假数据 */
        if (!s_wifi_connected) {
            if (wifi_wait_connected(WIFI_RETRY_INTERVAL_MS)) {
                ESP_LOGI(TAG, "Wi-Fi 已恢复");
                if (!s_time_synced) {
                    time_sync_start();
                    time_sync_wait(5000);
                }
            } else {
                ESP_LOGW(TAG, "Wi-Fi 仍未就绪，跳过本次上传");
                continue;
            }
        }

        char ts_device[48];
        make_device_timestamp(ts_device, sizeof(ts_device));

        if (upload_reading(&s, ts_device) == ESP_OK) {
            fail_streak = 0;
        } else {
            fail_streak++;
            ESP_LOGW(TAG, "连续上传失败 %lu 次", (unsigned long)fail_streak);
        }

        /* 摄像头：按 CAMERA_PERIOD_MS 周期性抓帧并上传（独立于加速度节奏）*/
#if CAMERA_ENABLE
        if (s_camera_ok) {
            int64_t now_us = esp_timer_get_time();
            if (now_us - last_cam_us >= (int64_t)CAMERA_PERIOD_MS * 1000) {
                last_cam_us = now_us;
                camera_fb_t *fb = camera_capture();
                if (fb) {
                    ESP_LOGI(TAG, "抓取一帧 %u bytes，上传中…",
                             (unsigned int)fb->len);
                    upload_frame(fb->buf, fb->len, ts_device);
                    esp_camera_fb_return(fb);
                }
            }
        }
#endif

        /* 精确控制周期：扣掉本次采集与上传耗时 */
        int64_t elapsed = (esp_timer_get_time() - t0) / 1000;
        int64_t remain = SAMPLE_PERIOD_MS - elapsed;
        if (remain > 0) {
            vTaskDelay(pdMS_TO_TICKS(remain));
        }
    }
}
