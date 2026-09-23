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
#include "esp_heap_caps.h"
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
#include "help_btn.h"
#include "wave.h"
#include "backlog.h"

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
/* 开机计数（NVS 里单调递增）。第 2 周起用它拼 boot_id；
 * 断网缓存（backlog）也用它当队列文件名的第一段排序键 ——
 * 重启后 uptime 会归零，只有它能把"重启前攒的帧"排在前面。 */
static uint32_t s_boot_cnt = 0;

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

/*
 * 生成"距现在 us_ago 微秒之前"的时刻，口径与 make_device_timestamp 一致。
 *
 * 【为什么需要它 —— 波形的时间轴跟别处不一样】
 *   波形要回答的是"样本之间隔多久"，是个**相对间隔**。
 *   wave.c 里每个样本只记了 esp_timer 微秒（开机以来的单调时刻），
 *   它不知道 SNTP 对时成没成功，也不该知道 —— 换算放在这里，这里才有 s_time_synced。
 *
 *   关键在于：t_first 和 t_last 是**用同一个钟、按同一套换算**得出来的，
 *   所以服务端拿它们做差是准的 —— 板钟差多少（哪怕差一整天）都自动抵消。
 *   这就是为什么波形可以用板端时刻摆横轴，而不违反"服务端不采信板端时间"那条铁律：
 *   那条铁律管的是**判定**（入库时刻、证据校验、超时归因），不是相对间隔。
 *
 *   没对时时同样退化成 uptime 秒数并标注 —— 服务端解析不出来会自己换一种摆法，
 *   绝不会把 "uptime+12.3s" 当成某个 1970 年的时刻。
 */
static void make_timestamp_ago(int64_t us_ago, char *buf, size_t len)
{
    if (us_ago < 0) {
        us_ago = 0;                 /* 时钟回绕或算出负数时夹到 0，不倒着走 */
    }
    if (s_time_synced) {
        struct timeval tv;
        gettimeofday(&tv, NULL);
        /* 先按整数秒/微秒分开减，避免 us_ago 超过 32 位 suseconds_t 的范围 */
        tv.tv_sec  -= (time_t)(us_ago / 1000000);
        tv.tv_usec -= (suseconds_t)(us_ago % 1000000);
        while (tv.tv_usec < 0) {
            tv.tv_usec += 1000000;
            tv.tv_sec  -= 1;
        }
        struct tm tm_info;
        localtime_r(&tv.tv_sec, &tm_info);
        char base[32];
        strftime(base, sizeof(base), "%Y-%m-%dT%H:%M:%S", &tm_info);
        snprintf(buf, len, "%s.%03ld+08:00", base, (long)(tv.tv_usec / 1000));
    } else {
        snprintf(buf, len, "uptime+%.3fs(time_not_synced)", us_ago / 1000000.0);
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
 * 断网补传（计划书 4.2）再多带三个头：
 *   X-Source          "backlog" —— 显式声明这是补传，不是刚拍的
 *   X-Buffered-Us     这一帧在 Flash 队列里待了多久（微秒）
 *   X-Backlog-Dropped 存它的时候，队列已因满而丢弃的更老帧数
 *   ★ 补传帧**绝不能带 X-Request-Id** —— 服务端会直接 400 拒收。
 *     拿一张断网时拍的旧图去当某次远程请求的观测，就是第 2 周防的"旧值冒充"。
 *
 * 【为什么把参数收成一个结构体】
 *   原来只有 request_id 一个可选参数，现在多了三个，再加下去
 *   调用处会出现 `upload_frame(buf, len, ts, ts, NULL, "backlog", -1, -1)`
 *   这种"数位置"的代码 —— 多一个参数就有一个传错的机会。
 *   结构体里字段有名字，传错会编译报错（类型不同）或一眼看出来。
 *
 * 失败不致命：下一帧会重试，不阻塞加速度上传。
 */
typedef struct {
    const char *request_id;       /* 命令触发时带；周期抓拍与补传都不带 */
    const char *source;           /* NULL=由服务端按有无 request_id 推断 */
    int64_t     buffered_us;      /* 补传：在队列里待了多久；<0 表示不适用 */
    int32_t     backlog_dropped;  /* 补传：存它时已丢了多少更老的帧；<0 不适用 */
} frame_meta_t;

static esp_err_t upload_frame(const uint8_t *buf, size_t len,
                              const char *ts_device, const char *capture_ts,
                              const frame_meta_t *meta)
{
    const char *request_id = meta ? meta->request_id : NULL;
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
    if (meta && meta->source && meta->source[0]) {
        esp_http_client_set_header(client, "X-Source", meta->source);
    }
    if (meta && meta->buffered_us >= 0) {
        char buf_us[32];
        snprintf(buf_us, sizeof(buf_us), "%lld", (long long)meta->buffered_us);
        esp_http_client_set_header(client, "X-Buffered-Us", buf_us);
    }
    if (meta && meta->backlog_dropped >= 0) {
        char buf_d[16];
        snprintf(buf_d, sizeof(buf_d), "%ld", (long)meta->backlog_dropped);
        esp_http_client_set_header(client, "X-Backlog-Dropped", buf_d);
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

/* ---------------- 三轴波形批量上传（第5周）----------------
 *
 * 一批 = WAVE_BATCH_SAMPLES 个样本（默认 100 点 @20Hz = 5 秒）。
 * 报文里带的每一个字段都有用处，没有一个是"顺手加上"的：
 *
 *   samples   原始 ADC 计数（**不是**换算后的 g）——
 *             换算是可逆的，服务端拿原始值可以按任意系数重算，
 *             反过来拿 g 值就永远回不到原始计数了。
 *   lsb_per_g 手册标称灵敏度（±8g 量程 = 1024）
 *   calib     实测标定系数（0.8078）
 *             ★ 这两个**分开报**，不要只报一个融合后的 scale：
 *               以后要回答"这个偏差是量程选错还是零点没标"，两个都得在。
 *   t_first / t_last  批内首末样本的板端时刻（只用于摆横轴，服务端不拿它做判定）
 *   dropped   攒这批期间丢掉的样本数。服务端靠它判断能不能用采样率反推时间轴 ——
 *             批间有洞时累加就不成立。**丢了就要报，不能抹平成 0。**
 *   batch_seq 批号（开机内递增）。服务端靠它判断批与批之间有没有断开。
 *
 * 失败不致命：下一批会重试，不阻塞其它通道。
 */
static esp_err_t upload_wave_batch(const wave_batch_t *b)
{
    cJSON *root = cJSON_CreateObject();
    if (root == NULL) {
        return ESP_ERR_NO_MEM;
    }

    cJSON_AddStringToObject(root, "device_id", DEVICE_ID);
    cJSON_AddStringToObject(root, "sensor", "qma7981_accel");
    cJSON_AddStringToObject(root, "unit", "g");
    cJSON_AddStringToObject(root, "boot_id", s_boot_id);
    cJSON_AddNumberToObject(root, "batch_seq", b->batch_seq);
    cJSON_AddNumberToObject(root, "hz", WAVE_HZ);
    cJSON_AddNumberToObject(root, "dropped", b->dropped);
    cJSON_AddNumberToObject(root, "lsb_per_g", QMA7981_LSB_PER_G);
    cJSON_AddNumberToObject(root, "calib", QMA7981_CALIB_SCALE);

    int64_t now_us = esp_timer_get_time();
    char t_first[48], t_last[48];
    make_timestamp_ago(now_us - b->t_first_us, t_first, sizeof(t_first));
    make_timestamp_ago(now_us - b->t_last_us, t_last, sizeof(t_last));
    cJSON_AddStringToObject(root, "t_first", t_first);
    cJSON_AddStringToObject(root, "t_last", t_last);

    cJSON *arr = cJSON_AddArrayToObject(root, "samples");
    if (arr == NULL) {
        cJSON_Delete(root);
        return ESP_ERR_NO_MEM;
    }
    for (size_t i = 0; i < b->n; i++) {
        cJSON *tri = cJSON_CreateArray();
        if (tri == NULL) {
            cJSON_Delete(root);
            return ESP_ERR_NO_MEM;
        }
        cJSON_AddItemToArray(tri, cJSON_CreateNumber(b->xyz[i * 3 + 0]));
        cJSON_AddItemToArray(tri, cJSON_CreateNumber(b->xyz[i * 3 + 1]));
        cJSON_AddItemToArray(tri, cJSON_CreateNumber(b->xyz[i * 3 + 2]));
        cJSON_AddItemToArray(arr, tri);
    }

    char *payload = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (payload == NULL) {
        return ESP_ERR_NO_MEM;
    }

    char url[192];
    snprintf(url, sizeof(url), "%s/api/waveform", SERVER_URL);

    esp_http_client_config_t cfg = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = WAVE_UPLOAD_TIMEOUT_MS,
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (client == NULL) {
        free(payload);
        return ESP_FAIL;
    }
    esp_http_client_set_header(client, "Content-Type", "application/json");

    /* 保留 esp_http_client_open() 的原始错误码，不要统一压成 ESP_FAIL（见 upload_frame 的注释）*/
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
            /*
             * 服务端会把算好的姿态回给我们。打到串口有两个用处：
             *   1) 现场演示时不用开网页就知道板子现在是什么姿态；
             *   2) 网页显示不对时，可以一眼看出是板端传错了还是服务端算错了。
             */
            char resp[256];
            int n = esp_http_client_read(client, resp, sizeof(resp) - 1);
            if (n < 0) {
                n = 0;
            }
            resp[n] = '\0';
            ESP_LOGI(TAG, "波形批次 #%lu 上传成功 (HTTP %d, %d 点, 丢 %lu)",
                     (unsigned long)b->batch_seq, status, (int)b->n,
                     (unsigned long)b->dropped);
            if (n > 0) {
                ESP_LOGI(TAG, "  服务端判定: %s", resp);
            }
            /* 上面最多读走 256 字节，剩下的必须排空（见 http_drain_body 注释）*/
            http_drain_body(client);
        } else {
            ESP_LOGE(TAG, "波形批次 #%lu 服务器返回异常状态码 %d",
                     (unsigned long)b->batch_seq, status);
            err = ESP_FAIL;
        }
    } else {
        ESP_LOGE(TAG, "波形批次 #%lu 上传失败: %s",
                 (unsigned long)b->batch_seq, esp_err_to_name(err));
    }

    esp_http_client_close(client);
    esp_http_client_cleanup(client);
    free(payload);
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
    s_boot_cnt = count;                 /* 断网缓存也要用它，见 s_boot_cnt 的注释 */
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
    /*
     * ★ source 明确写 "web_manual"，**不能**写 "backlog"。
     *   写错了服务端会 400 拒收（补传帧不许绑 request_id，见 upload_frame 注释）。
     *   buffered_us / backlog_dropped 传 -1 表示"不适用"—— 这一帧刚拍完就发，
     *   没有"在队列里待了多久"这回事，不能填 0（0 也是"待了 0 微秒"，但语义不同）。
     */
    const frame_meta_t meta = {
        .request_id      = request_id,
        .source          = "web_manual",
        .buffered_us     = -1,
        .backlog_dropped = -1,
    };
    upload_frame(fb->buf, fb->len, capture_ts, capture_ts, &meta);
    esp_camera_fb_return(fb);
#else
    ESP_LOGE(TAG, "指令 %s 无法执行：固件编译时未启用摄像头", request_id);
    command_ack(request_id, "FAILED", "camera disabled in build");
#endif
}

/* ================= 断网补传（计划书 4.2）=================
 *
 * 【为什么必须有这个 —— 原来这里是漏的】
 *   断网时主循环直接 `continue` 跳过整个循环体，**连帧都不抓**。
 *   网络恢复之后那段时间就是一片空白，谁也补不回来。
 *   可是摄像头抓帧根本不需要网络 —— 白白浪费了设备本来就有的能力。
 *   计划书把这一条标成"不是可选项"，因为"设备一脱网数据就丢"
 *   和"这是一个无人值守的采集终端"这个前提是矛盾的。
 *
 * 【补传帧和"刚拍的帧"差在哪 —— 这是整个功能的题眼】
 *   差的是**采集时刻**。补传帧的采集时刻在过去，可能是几分钟以前。
 *   所以三件事必须做全：
 *     ① 带 X-Source: backlog        —— 显式声明，不靠服务端猜
 *     ② 带 X-Capture-Ts = 采集时刻  —— 不是上传时刻（服务端靠它算"在队列里待了多久"）
 *     ③ **不带 X-Request-Id**        —— 服务端会 400 拒收，理由见 upload_frame 注释
 *   缺任何一条，画廊里就会出现"看起来属于某次请求的旧图"，
 *   也就是第 2 周整周在防的那个"旧值冒充新采集"。
 *
 * 【补传帧还带一个 X-Buffered-Us，而不是让服务端自己算】
 *   服务端当然可以拿 (ts_server - capture_ts) 算，但那依赖板钟走得准。
 *   板钟在对时之前是 1970 年，对时之后也可能偏 —— 而这个差值
 *   （在队列里待了多久）板端用**同一个单调钟**相减就能精确得到，
 *   不受板钟准不准影响。能算准的一方来算，算不准的一方别猜。
 */
#if CAMERA_ENABLE

/* 上一次"断网抓帧入队"的板端时刻。初值取负一个周期 → 第一次判定立刻通过，
 * 即断网后马上抓一帧（断网窗口可能很短，别等到第一个周期才动手）。 */
static int64_t s_last_offline_cam_us =
    -((int64_t)BACKLOG_OFFLINE_PERIOD_MS * 1000);

/* 补传用的读缓冲：惰性申请一次，之后一直复用。
 * 刻意不用 esp_camera_fb_get() 去借缓冲 —— 那个函数会**真的再抓一帧**。
 * 补传一张旧图却顺手拍一张新图，既浪费又打乱周期抓拍的节奏。 */
static uint8_t *s_replay_buf = NULL;

static uint8_t *replay_buf_get(void)
{
    if (s_replay_buf != NULL) {
        return s_replay_buf;
    }
    s_replay_buf = (uint8_t *)heap_caps_malloc(BACKLOG_MAX_FRAME_BYTES,
                                               MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (s_replay_buf == NULL) {
        /* 申请不到就如实说，不要静默降级成"补传悄悄不工作了" */
        ESP_LOGE(TAG, "补传读缓冲申请失败（%d 字节），补传无法进行",
                 (int)BACKLOG_MAX_FRAME_BYTES);
    }
    return s_replay_buf;
}

/* 把队列里的一帧补传出去。
 *   capture_uptime_s  该帧采集时刻的板端 uptime（秒）
 *   now_us            当前板端 uptime（微秒） */
static esp_err_t upload_backlog_frame(const uint8_t *buf, size_t len,
                                      int64_t capture_uptime_s, int64_t now_us)
{
    /*
     * "在队列里待了多久" = 现在 - 采集时。
     * 两个时刻都取自同一个单调钟（esp_timer），所以这个差值就是真实经过的时间，
     * 板钟准不准都不影响它 —— 跟波形时间轴用的是同一套道理。
     */
    int64_t buffered_us = now_us - capture_uptime_s * 1000000;
    if (buffered_us < 0) {
        buffered_us = 0;            /* 时钟回绕时夹到 0，不倒着走 */
    }

    /*
     * 采集时刻按"距现在 buffered_us 微秒之前"还原：
     *   已对时 → 真实的 ISO 时刻（且是**过去**的时刻，这正是它该有的样子）
     *   未对时 → uptime+<秒数>s(time_not_synced)
     * 所以即使整段断网期间都没对时，这帧"是多久以前拍的"也没有丢。
     */
    char capture_ts[48];
    make_timestamp_ago(buffered_us, capture_ts, sizeof(capture_ts));

    char ts_device[48];
    make_device_timestamp(ts_device, sizeof(ts_device));

    const frame_meta_t meta = {
        .request_id      = NULL,    /* ★ 补传帧绝不能绑 request_id */
        .source          = "backlog",
        .buffered_us     = buffered_us,
        .backlog_dropped = (int32_t)backlog_dropped_total(),
    };
    return upload_frame(buf, len, ts_device, capture_ts, &meta);
}

/* 断网期间：按 BACKLOG_OFFLINE_PERIOD_MS 的节奏抓帧并存入 Flash 队列。
 *
 * 【为什么离线抓帧要降频】
 *   分区 3MB，800×600 的 JPEG 一帧约 40~80KB（估），装得下 35~70 帧
 *   （条数软上限 BACKLOG_MAX_FRAMES=64，空间先到就先卡住）。
 *   沿用在线时的 2 秒间隔，一两分钟就把队列写满、之后全被丢掉 ——
 *   结果是"断网 5 分钟只留下最后 2 分钟"，覆盖窗口反而更短。
 *   放宽到 10 秒，换来约 6~11 分钟的覆盖窗口。
 *   这是**有意的取舍**：宁可时间上稀一点，也不要覆盖窗口短到没意义。
 *   真需要高密度就该换更大的分区，而不是把间隔调小。
 *   ★ 上面这个窗口是按估算算的，真机上要实测一帧实际多大再复核。 */
static void backlog_capture_if_due(int64_t now_us)
{
    if (!s_camera_ok) {
        return;
    }
    if (now_us - s_last_offline_cam_us <
        (int64_t)BACKLOG_OFFLINE_PERIOD_MS * 1000) {
        return;
    }
    s_last_offline_cam_us = now_us;

    if (!backlog_ready()) {
        ESP_LOGW(TAG, "断网中，但 Flash 队列不可用 —— 本帧只能丢弃（如实记录，不假装存下了）");
        return;
    }

    camera_fb_t *fb = camera_capture();
    if (fb == NULL) {
        ESP_LOGW(TAG, "断网中，抓帧失败，本帧丢弃");
        return;
    }

    /*
     * 采集时刻用**板端 uptime 的秒数**，不是对时后的墙上时间 ——
     * 断网时 SNTP 必然失败，此刻根本没有可信的墙上时间。
     * uptime 是单调钟，联网后拿 (那时的 uptime - 这时的 uptime) 反推
     * "这一帧是多久以前拍的"，再套上对时后的当前时刻就还原出真实时刻。
     * 这就是为什么采集时刻要写进队列文件名。
     */
    int64_t capture_uptime_s = now_us / 1000000;
    esp_err_t err = backlog_put(fb->buf, fb->len, s_boot_cnt, capture_uptime_s);
    esp_camera_fb_return(fb);

    if (err == ESP_OK) {
        ESP_LOGI(TAG, "断网中：已缓存一帧到 Flash（队列 %d 帧，累计丢弃 %lu 帧）",
                 backlog_count(), (unsigned long)backlog_dropped_total());
    } else {
        ESP_LOGW(TAG, "断网中：缓存失败 %s（累计丢弃 %lu 帧）",
                 esp_err_to_name(err), (unsigned long)backlog_dropped_total());
    }
}

/* 联网后：把 Flash 队列里最老的几帧补传出去（先进先出）。
 * 返回本次成功送达的帧数。
 *
 * 【为什么每轮只传 1 帧（BACKLOG_REPLAY_PER_LOOP 默认 1）】
 *   每传一帧都要一次完整 HTTP 往返，本机这条链路抖动到 770ms。
 *   一轮里塞满补传，周期上报和命令轮询就被挤没了 ——
 *   而"暂停周期上报后命令通道仍能出新数据"是课程明确的检查项，
 *   为了补传把它挤挂是本末倒置。
 *   分开传的代价只是清空队列慢一点：离线按 10 秒攒、联网按 ~2 秒一轮清，
 *   清空速度本来就是积攒速度的 5 倍左右，队列不会越积越多。
 *
 * 【和在线抓帧的关系】
 *   补传不清空完也照常抓新帧 —— 两条流在画廊里是分开标注的
 *   （补传帧有「断网补传」徽标和三个时间），所以时间上交错出现
 *   不会被误读成"板子时间乱了"。 */
static int backlog_replay(int max_frames)
{
    if (!backlog_ready()) {
        return 0;
    }
    int sent = 0;
    for (int i = 0; i < max_frames; i++) {
        int queued = backlog_count();
        if (queued <= 0) {
            break;
        }
        uint8_t *buf = replay_buf_get();
        if (buf == NULL) {
            break;
        }

        size_t len = 0;
        int64_t capture_uptime_s = 0;
        char name[64];
        esp_err_t err = backlog_peek_oldest(buf, BACKLOG_MAX_FRAME_BYTES, &len,
                                            &capture_uptime_s, name, sizeof(name));
        if (err != ESP_OK) {
            /* 队列空、或队首是个坏条目（backlog.c 内部会把它删掉）。
             * 不在这里重试 —— 下一轮循环自然会重新取队首。 */
            ESP_LOGW(TAG, "取队首缓存帧失败: %s（队列 %d 帧）",
                     esp_err_to_name(err), queued);
            break;
        }

        int64_t now_us = esp_timer_get_time();
        int64_t buffered_s = (now_us - capture_uptime_s * 1000000) / 1000000;
        if (buffered_s < 0) {
            buffered_s = 0;
        }
        ESP_LOGI(TAG, "补传缓存帧 %s（%u bytes，采集于 %lld 秒前，队列还剩 %d 帧）",
                 name, (unsigned int)len, (long long)buffered_s, queued);

        if (upload_backlog_frame(buf, len, capture_uptime_s, now_us) != ESP_OK) {
            /* ★ 失败就不删。留着下一轮再试 ——
             *   先删后传的话，传失败这一帧就凭空没了，
             *   而 dropped 计数还是 0，服务端会以为数据是完整的。
             *   这比丢帧本身更糟：它把"丢过数据"这个事实也一起丢了。 */
            ESP_LOGW(TAG, "补传失败，该帧留在队列里等下一轮重试");
            break;
        }
        /* 只有真的送达了才删。 */
        backlog_drop_oldest();
        sent++;
    }
    return sent;
}

#else  /* !CAMERA_ENABLE */

/* 没启用摄像头就没有帧可缓存。明确写成空函数，
 * 调用处不用再套一层 #if —— 少一处条件，就少一处将来改配置时忘改的地方。 */
static void backlog_capture_if_due(int64_t now_us) { (void)now_us; }
static int  backlog_replay(int max_frames) { (void)max_frames; return 0; }

#endif /* CAMERA_ENABLE */

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

    /* 1.6) 第3周：按键 + 本地反馈。
     * 刻意放在连 Wi-Fi **之前** —— 本地反馈（LED）不该依赖网络：
     * 断网时按下去，板子也要能立刻用灯告诉用户"我收到了"，
     * 然后才是"发不出去"。顺序反过来的话，断网时按键会毫无反应。 */
#if HELP_ENABLE
    help_btn_init(s_boot_id);
#endif

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

    /* 2.6) 断网补传用的 Flash 队列（计划书 4.2）。
     * 必须在主循环之前挂载好 —— 否则断网时 backlog_ready() 是 false，
     * 那段时间的帧只能丢，而这恰恰是这个功能要避免的情况。
     * 失败只降级：其余功能（加速度/命令/求助/波形）完全不受影响。 */
    esp_err_t bk_err = backlog_init();
    if (bk_err != ESP_OK) {
        /* ESP_ERR_NOT_SUPPORTED = 编译时被 BACKLOG_ENABLE 关掉了（不是故障）；
         * 其它错误码 = 分区挂载真的失败了。把错误码原样打出来，
         * 免得"故意关掉"和"坏掉了"在日志里长得一样。 */
        ESP_LOGW(TAG, "断网缓存未启用（%s）：断网期间的帧会被丢弃，其余功能不受影响",
                 esp_err_to_name(bk_err));
    } else {
        ESP_LOGI(TAG, "断网补传已就绪：分区 %s，当前队列 %d 帧，离线抓帧间隔 %d ms",
                 BACKLOG_PARTITION_LABEL, backlog_count(), BACKLOG_OFFLINE_PERIOD_MS);
        if (backlog_count() > 0) {
            /* 上次开机断网时攒下的帧，这次开机联网后会被补传出去。
             * 这属于**正常情况**，不是异常 —— 日志里说清楚，免得排查时误判。 */
            ESP_LOGI(TAG, "  队列里有 %d 帧是上次开机遗留的，联网后会自动补传",
                     backlog_count());
        }
    }

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
#if HELP_ENABLE
    ESP_LOGI(TAG, "按键求助通道已启用：短按 GPIO%d 发起/取消，长按 %dms 重发",
             HELP_BTN_GPIO, HELP_LONG_PRESS_MS);
#endif

    /*
     * 第5周：启动波形采样任务。
     *
     * 【为什么放在这里，而不是更早的硬件初始化之后】
     *   采样任务是 20Hz 连轴转的，而环形缓冲只有 25.6 秒容量。
     *   上面连 Wi-Fi（最多 20s）+ 对时（最多 10s）加起来可能超过 25 秒 ——
     *   如果那会儿就开始采样，缓冲一开张就溢满，第一批报文直接带着 dropped 出门，
     *   服务端就只能退回到最差的那条时间轴。等网络就绪了再开始采，
     *   这段时间本来也传不出去，早采没有意义。
     */
#if WAVE_ENABLE
    if (wave_init() != ESP_OK) {
        ESP_LOGW(TAG, "波形采样启动失败，网页上的示波器会没有数据（其余功能不受影响）");
    } else {
        ESP_LOGI(TAG, "波形采样已启用：%d Hz × %d 点 = 每 %d 秒一批，缓冲 %d 点",
                 WAVE_HZ, WAVE_BATCH_SAMPLES, WAVE_BATCH_SAMPLES / WAVE_HZ,
                 WAVE_RING_SAMPLES);
    }
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
         * 0) 断网期间的抓帧缓存（计划书 4.2）。
         *
         * 刻意放在**所有上传逻辑之前**，也刻意**不挂在传感器读取之后** ——
         * 抓帧缓存和"加速度计读到了没有"是两件不相干的事。
         * 如果放在 qma7981_read 之后，传感器一旦读失败，那一轮的
         * `continue` 会连带把抓帧也停掉：一个子系统的故障悄悄让
         * 另一个子系统也不工作，而串口上只看得到「读取传感器失败」——
         * 排查时根本想不到是它连累的。
         */
        if (!s_wifi_connected) {
            backlog_capture_if_due(esp_timer_get_time());
        }

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

        /*
         * 0.5) 第3周：按键求助通道。
         *
         * 按键本身在 20ms 的定时器里采集（见 help_btn.c），这里只负责
         * "取走待办动作并执行 HTTP"。同样与周期上报解耦 ——
         * 暂停周期上报时按键求助照样能用。
         *
         * 注意：这个函数内部不做重试。按一次就是一次，失败就如实报失败 ——
         * 偷偷重发会让"本地确认"和"VPS 接收"的时间差变得不可解释。
         */
#if HELP_ENABLE
        if (s_wifi_connected) {
            help_action_t hact = help_btn_poll();
            if (hact != HELP_ACT_NONE) {
                ESP_LOGI(TAG, "按键动作已处理: %d，当前板端状态 %s",
                         (int)hact, help_state_name(help_btn_get_state()));
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
                /*
                 * 注意这里 `continue` 跳过的是**上传**，不是抓帧 ——
                 * 抓帧缓存已经在循环开头做过了（见步骤 0）。
                 * 断网期间照常抓帧存进 Flash 队列，等网络回来自动补传
                 * （计划书 4.2「断网缓冲不是可选项」）。
                 * 原来这里直接 continue 就真的什么都不做了，那段时间一片空白。
                 */
                continue;
            }
        }

        /*
         * 联网了：先把 Flash 里攒下的旧帧补传出去（先进先出，每轮最多 1 帧）。
         *
         * 刻意排在周期上报之前 —— 补传的是**更早**的数据，
         * 先送老的再送新的，服务端看到的时间序列才是顺着来的。
         */
        int replayed = backlog_replay(BACKLOG_REPLAY_PER_LOOP);
        if (replayed > 0) {
            ESP_LOGI(TAG, "本轮补传 %d 帧，队列还剩 %d 帧",
                     replayed, backlog_count());
        }

        char ts_device[48];
        make_device_timestamp(ts_device, sizeof(ts_device));

        if (upload_reading(&s, ts_device) == ESP_OK) {
            fail_streak = 0;
        } else {
            fail_streak++;
            ESP_LOGW(TAG, "连续上传失败 %lu 次", (unsigned long)fail_streak);
        }

        /*
         * 第5周：波形批次上传。
         *
         * 不用自己计时 —— wave_take_batch() 攒不够一批会返回 NULL，
         * 20Hz × 100 点自然就是 5 秒一批，它自己就是节拍器。
         *
         * 【发失败了为什么不重发】
         *   批号在**取走的那一刻**就自增过了（见 wave.c），所以这一批没送达时，
         *   服务端会看到批号从 6 跳到 8 —— 它据此知道"中间少了一段"，
         *   不会把 7 和 9 当成连续的。反过来若改成"发成功才自增"，
         *   失败就会留下一个看不出来的洞，时间轴会被摆错还显得很正常。
         *   波形是**过程数据**，丢一批只少一段画面，不值得为它重试到阻塞主循环。
         */
#if WAVE_ENABLE
        if (s_wifi_connected) {
            const wave_batch_t *wb = wave_take_batch();
            if (wb != NULL && upload_wave_batch(wb) != ESP_OK) {
                ESP_LOGW(TAG, "波形批次 #%lu 未送达（服务端会看到批号断档）",
                         (unsigned long)wb->batch_seq);
            }
        }
#endif

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
                    /*
                     * 周期抓拍：不带 request_id、也不是补传。
                     * source 明确写 "periodic"，不留给服务端按"有没有 request_id"去推断 ——
                     * 推断虽然也能得出正确结果，但显式声明让"这一帧从哪来"
                     * 在链路上就是可查的，而不是靠约定推出来的。
                     */
                    const frame_meta_t meta = {
                        .request_id      = NULL,
                        .source          = "periodic",
                        .buffered_us     = -1,
                        .backlog_dropped = -1,
                    };
                    upload_frame(fb->buf, fb->len, cap_ts, cap_ts, &meta);
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
