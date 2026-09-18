/*
 * main.c —— AI交互课 设备端固件
 *
 * 职责：
 *   1) 读取 ESP32-S3-EYE 板载 QMA7981 三轴加速度计（真实测量值）
 *   2) 通过 Wi-Fi 把「数值 + 单位 + 时间戳 + 设备编号」POST 到自建服务器
 *   3) 每秒一次；网络异常时只重连、不重启，不产生假数据
 *   4) 【第2周】按周期轮询服务器的「远程采集指令」，取到就立即回执、
 *      抓一帧带上 request_id / capture_ts / boot_id / seq 回传
 *
 * 第2周为什么要多带三个字段：
 *   服务器要能回答「这张图到底是不是这次拍的那张」。只靠 request_id 不够 ——
 *   如果板子把上一次拍的同一张图重传一次，请求对得上、时间也可能对得上。
 *   所以还要 boot_id（本次开机标识）+ seq（开机内递增序号）来证明"确实新拍了一张"。
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

/* ---- 第2周：新鲜度标识 ----
 * s_boot_id：本次开机的标识，存在 NVS 里的开机计数 + MAC 尾号，重启必变。
 * s_seq    ：开机内递增序号，随每一帧上传自增；重启后归零（配合 boot_id 使用）。
 * 服务器用 (boot_id, seq) 的组合判断"这一帧是不是新拍的"，见 server.py 的 E3 校验。
 */
static char s_boot_id[24] = {0};
static uint32_t s_seq = 0;

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

/* 把 HTTP 响应体读干净，再关闭连接。
 *
 * 【坑·本工程实测踩到，且极具迷惑性】
 * 只 fetch_headers 就把 socket 关掉会出大问题：服务端每个响应都带一小段
 * JSON（Content-Length 明确），客户端不读走的话，接收缓冲区里还留着未读
 * 数据，协议栈会直接发 RST 而不是正常四次挥手。后果有两个：
 *   1) 服务端记一条 ConnectionResetError，看起来像客户端崩了；
 *   2) 客户端这边 fd 迟迟回收不掉，很快撞上
 *      errno=Connection already in progress / esp-tls: select() timeout。
 *
 * 现象极具迷惑性：**服务端日志里每秒都是 201，板子串口却一路刷
 * "连续上传失败 N 次"** —— 数据其实已经入库了，是板子把成功的请求判成了
 * 失败。排空响应体之后，两边结论就一致了。
 */
static void http_drain_body(esp_http_client_handle_t client)
{
    char buf[64];
    while (esp_http_client_read(client, buf, sizeof(buf)) > 0) {
        /* 只为排空，内容丢弃 */
    }
}

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
        /* 响应体必须读走再关连接，否则协议栈会发 RST（见 http_drain_body 注释）*/
        http_drain_body(client);
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
 * 用自定义请求头携带设备编号、板端时间与新鲜度标识，便于服务器落库、关联与校验：
 *
 *   X-Device-Id   设备编号
 *   X-Ts-Device   上传时刻
 *   X-Capture-Ts  采集时刻（E2 时序校验用：必须晚于指令下发时刻）
 *   X-Boot-Id     本次开机标识（E3）
 *   X-Seq         开机内递增序号（E3）
 *   X-Request-Id  本次所属请求 —— 只有命令触发的帧才带；
 *                 周期性抓拍不带它，服务器那边就只入库、不参与命令闭环。
 *
 * 失败不致命：下一帧会重试，不阻塞加速度上传。
 */
static esp_err_t upload_frame(const uint8_t *buf, size_t len,
                              const char *ts_device, const char *capture_ts,
                              const char *request_id)
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

    /*
     * 序号在真正要发的时候才自增，保证「seq 的大小顺序 == 服务器收到的顺序」。
     * 这是 E3 校验成立的前提：如果先自增后上传，一旦上传失败或乱序，
     * 服务器看到的序号就不单调了。
     */
    s_seq++;

    char seq_buf[16];
    snprintf(seq_buf, sizeof(seq_buf), "%lu", (unsigned long)s_seq);

    esp_http_client_set_header(client, "Content-Type", "image/jpeg");
    esp_http_client_set_header(client, "X-Device-Id", DEVICE_ID);
    esp_http_client_set_header(client, "X-Ts-Device", ts_device);
    esp_http_client_set_header(client, "X-Capture-Ts",
                               (capture_ts && capture_ts[0]) ? capture_ts
                                                             : ts_device);
    esp_http_client_set_header(client, "X-Boot-Id", s_boot_id);
    esp_http_client_set_header(client, "X-Seq", seq_buf);
    if (request_id && request_id[0]) {
        esp_http_client_set_header(client, "X-Request-Id", request_id);
    }

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
            ESP_LOGI(TAG, "帧上传成功 (HTTP %d, %u bytes, seq=%lu)", status,
                     (unsigned int)len, (unsigned long)s_seq);
            /*
             * 命令触发的帧：服务器会在响应里带回证据校验结论。
             * 把它打到串口，就能当场看出「这次算不算成功、不成功是差哪条证据」，
             * 不用再去翻服务器日志对时间。周期性抓拍的响应没有这个字段，跳过。
             */
            if (request_id && request_id[0]) {
                char resp[192];
                int n = esp_http_client_read(client, resp, sizeof(resp) - 1);
                if (n > 0) {
                    resp[n] = '\0';
                    ESP_LOGI(TAG, "  指令 %s 证据校验结论: %s", request_id, resp);
                }
            }
            /* 上面最多只读走 192 字节，剩下的必须排空（见 http_drain_body 注释）*/
            http_drain_body(client);
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

/* ---------------- 远程采集指令通道（第2周）----------------
 *
 * 三个函数的职责划分：
 *   boot_id_init()          生成"本次开机"的标识（存 NVS 的开机计数 + MAC 尾号）
 *   command_poll()          向服务器要一条待执行指令；没有待执行指令返回 false
 *   command_ack()           回执。取到指令要**立即**回执，抓图失败再回执一次 FAILED
 *   handle_remote_command() 把上面几步和抓帧、回传串起来
 *
 * 为什么回执要"立即"：
 *   服务器只有收到回执，才能把状态从 PENDING 推到 RECEIVED。
 *   如果等到抓完图再回执，"设备已接收"和"已完成"之间的时间差就没了，
 *   出问题时看不出是"没收到指令"还是"收到了但抓图失败"。
 */
static void boot_id_init(void)
{
    uint32_t count = 0;
    nvs_handle_t h;
    if (nvs_open("app", NVS_READWRITE, &h) == ESP_OK) {
        if (nvs_get_u32(h, "boot_cnt", &count) != ESP_OK) {
            count = 0;                  /* 首次开机，NVS 里还没有这个键 */
        }
        count++;
        nvs_set_u32(h, "boot_cnt", count);
        nvs_commit(h);
        nvs_close(h);
    }
    uint8_t mac[6] = {0};
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    snprintf(s_boot_id, sizeof(s_boot_id), "%02x%02x%02x-boot%lu",
             mac[3], mac[4], mac[5], (unsigned long)count);
    ESP_LOGI(TAG, "本次开机标识 boot_id = %s（重启后会变，供服务器做新鲜度校验）",
             s_boot_id);
}

/* 取一条待执行指令。取到 -> true 并填好 request_id；没有 -> false（这是常态）。 */
static bool command_poll(char *request_id, size_t len)
{
    char url[256];
    snprintf(url, sizeof(url), "%s/api/command/poll?device_id=%s",
             SERVER_URL, DEVICE_ID);

    esp_http_client_config_t cfg = {
        .url = url,
        .method = HTTP_METHOD_GET,
        .timeout_ms = CMD_TIMEOUT_MS,
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (client == NULL) {
        return false;
    }

    /*
     * 响应体用固定大小缓冲一次读完。
     * 服务器那边专门做了精简载荷（只有 request_id / action / ttl 等执行必需字段），
     * 就是为了让这里不用动态扩容 —— MCU 上的内存是实打实的约束。
     */
    char body[512];
    int rd = 0;
    esp_err_t err = esp_http_client_open(client, 0);
    if (err == ESP_OK) {
        err = esp_http_client_fetch_headers(client);
    }
    if (err == ESP_OK && esp_http_client_get_status_code(client) == 200) {
        while (rd < (int)sizeof(body) - 1) {
            int n = esp_http_client_read(client, body + rd,
                                         sizeof(body) - 1 - rd);
            if (n <= 0) {
                break;
            }
            rd += n;
        }
    } else if (err != ESP_OK) {
        /* 取指令失败不影响周期上报，下一轮再试即可，不必刷错误日志 */
        ESP_LOGW(TAG, "取指令失败: %s（不影响周期上报，下轮重试）",
                 esp_err_to_name(err));
    }
    body[rd] = '\0';
    esp_http_client_close(client);
    esp_http_client_cleanup(client);

    bool got = false;
    cJSON *root = cJSON_Parse(body);
    if (root != NULL) {
        cJSON *cmd = cJSON_GetObjectItem(root, "command");
        /* 没有待执行指令时服务器返回 {"command": null}，cJSON_IsObject 为假 */
        if (cJSON_IsObject(cmd)) {
            cJSON *rid = cJSON_GetObjectItem(cmd, "request_id");
            if (cJSON_IsString(rid) && rid->valuestring[0] != '\0') {
                strncpy(request_id, rid->valuestring, len - 1);
                request_id[len - 1] = '\0';
                got = true;
            }
        }
        cJSON_Delete(root);
    }
    return got;
}

/* 回执。state 传 NULL 表示"已接收，开始执行"；传 "FAILED" 表示执行不了。 */
static esp_err_t command_ack(const char *request_id, const char *state,
                             const char *reason)
{
    cJSON *root = cJSON_CreateObject();
    if (root == NULL) {
        return ESP_ERR_NO_MEM;
    }
    char ts_device[48];
    make_device_timestamp(ts_device, sizeof(ts_device));

    cJSON_AddStringToObject(root, "request_id", request_id);
    cJSON_AddStringToObject(root, "device_id", DEVICE_ID);
    cJSON_AddStringToObject(root, "device_ts", ts_device);
    cJSON_AddStringToObject(root, "boot_id", s_boot_id);
    cJSON_AddNumberToObject(root, "seq", s_seq);
    if (state != NULL) {
        cJSON_AddStringToObject(root, "state", state);
    }
    if (reason != NULL) {
        cJSON_AddStringToObject(root, "reason", reason);
    }

    char *payload = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (payload == NULL) {
        return ESP_ERR_NO_MEM;
    }

    char url[192];
    snprintf(url, sizeof(url), "%s/api/command/ack", SERVER_URL);
    esp_http_client_config_t cfg = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = CMD_TIMEOUT_MS,
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
        err = (written < 0) ? ESP_FAIL : esp_http_client_fetch_headers(client);
    }
    if (err == ESP_OK) {
        int status = esp_http_client_get_status_code(client);
        /* 响应体必须读走再关连接，否则协议栈会发 RST（见 http_drain_body 注释）*/
        http_drain_body(client);
        if (status >= 200 && status < 300) {
            ESP_LOGI(TAG, "回执成功 (HTTP %d): %s -> %s", status, request_id,
                     (state != NULL) ? state : "EXECUTING");
        } else {
            ESP_LOGE(TAG, "回执失败，服务器返回 %d", status);
            err = ESP_FAIL;
        }
    } else {
        ESP_LOGE(TAG, "回执请求失败: %s", esp_err_to_name(err));
    }
    esp_http_client_close(client);
    esp_http_client_cleanup(client);
    free(payload);
    return err;
}

/* 完整执行一条远程指令：立即回执 -> 抓帧 -> 带上 request_id 回传 -> 打印校验结论 */
static void handle_remote_command(void)
{
    char request_id[64];
    if (!command_poll(request_id, sizeof(request_id))) {
        return;                     /* 没有待执行指令，正常情况，不打印日志 */
    }
    ESP_LOGI(TAG, ">>> 取到远程指令 %s，开始执行", request_id);

    /* 第一步：立即回执。这是「设备已接收」状态唯一的证据来源 */
    command_ack(request_id, NULL, NULL);

#if CAMERA_ENABLE
    if (!s_camera_ok) {
        ESP_LOGE(TAG, "指令 %s 无法执行：摄像头不可用", request_id);
        command_ack(request_id, "FAILED", "camera not available");
        return;
    }
    camera_fb_t *fb = camera_capture();
    if (fb == NULL) {
        ESP_LOGE(TAG, "指令 %s 无法执行：抓帧失败", request_id);
        command_ack(request_id, "FAILED", "capture failed");
        return;
    }
    /*
     * 采集时刻单独取一次，而不是复用上传时刻。
     * E2 校验比的就是"采集时刻"和"指令下发时刻"的先后 ——
     * 如果直接拿上传时刻充数，那它必然晚于下发时刻，校验就成了走过场。
     */
    char capture_ts[48];
    make_device_timestamp(capture_ts, sizeof(capture_ts));
    ESP_LOGI(TAG, "指令 %s 抓取一帧 %u bytes，带 request_id 回传", request_id,
             (unsigned int)fb->len);
    upload_frame(fb->buf, fb->len, capture_ts, capture_ts, request_id);
    esp_camera_fb_return(fb);
#else
    ESP_LOGE(TAG, "指令 %s 无法执行：固件编译时未启用摄像头", request_id);
    command_ack(request_id, "FAILED", "camera disabled in build");
#endif
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

    /* 1.5) 生成本次开机标识（供服务器做 E3 新鲜度校验），必须在 NVS 就绪之后 */
    boot_id_init();

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
#if CMD_ENABLE
    ESP_LOGI(TAG, "远程指令通道已启用，取指令周期 %d ms（与周期上报相互独立）",
             CMD_POLL_INTERVAL_MS);
#endif

    /* 6) 主循环 */
    uint32_t fail_streak = 0;
#if CAMERA_ENABLE
    int64_t last_cam_us = -((int64_t)CAMERA_PERIOD_MS * 1000);  /* 首帧立即抓 */
#endif
#if CMD_ENABLE
    int64_t last_poll_us = -((int64_t)CMD_POLL_INTERVAL_MS * 1000); /* 首轮立即取 */
#endif
    while (true) {
        int64_t t0 = esp_timer_get_time();

        /*
         * 0) 命令通道：按自己的节拍取指令。
         *
         * 刻意放在周期采集之前、且用独立的计时变量 —— 这样即使把
         * SAMPLE_PERIOD_MS 调大、或者暂停周期上报，命令通道依然照常工作。
         * 课程明确的检查项就是这一条：「暂停周期上报后，命令触发仍能出新数据」。
         */
#if CMD_ENABLE
        if (s_wifi_connected) {
            int64_t now_us = esp_timer_get_time();
            if (now_us - last_poll_us >= (int64_t)CMD_POLL_INTERVAL_MS * 1000) {
                last_poll_us = now_us;
                handle_remote_command();
            }
        }
#endif

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
                    /* 周期性抓拍不带 request_id：服务器那边只入库，不参与命令闭环 */
                    char cap_ts[48];
                    make_device_timestamp(cap_ts, sizeof(cap_ts));
                    ESP_LOGI(TAG, "周期抓取一帧 %u bytes，上传中…",
                             (unsigned int)fb->len);
                    upload_frame(fb->buf, fb->len, cap_ts, cap_ts, NULL);
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
