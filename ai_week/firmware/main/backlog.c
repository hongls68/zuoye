/*
 * backlog.c —— 断网期间的帧缓冲队列（实现）
 *
 * 设计说明见 backlog.h。这里只强调三件事：
 *
 * 1) **队列顺序由文件名决定**，不维护索引文件。
 *    索引文件的问题是"两份数据要保持一致"，写索引时掉电就烂了。
 *    文件名承载全部元数据，就没有这个窗口。
 *
 * 2) **写不下时丢最老的，并如实计数**。
 *    和波形环形缓冲同一个原则：用户关心"刚才发生了什么"，
 *    所以丢最老。丢了多少会随下一帧上报，服务端据此知道批间有洞。
 *
 * 3) **先传成功再删**（调用方负责顺序）。
 *    先删后传的话，传失败就等于凭空丢了一帧，而 dropped 还是 0 ——
 *    服务端会以为数据完整。这是"诚实计数"的关键一环。
 */
#include <dirent.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "esp_log.h"
#include "esp_spiffs.h"

#include "app_config.h"
#include "backlog.h"

static const char *TAG = "backlog";

#define BACKLOG_NAME_LEN   32      /* SPIFFS_OBJ_NAME_LEN 默认 32，名字不能超 */

static bool     s_ready = false;
static uint32_t s_dropped = 0;      /* 累计因满而丢弃的帧数 */
static uint32_t s_next_seq = 0;     /* 本次开机内的入队序号 */

/* ---------------- 文件名编解码 ---------------- */

/*
 * BBBBBB_SSSSSSSS_TTTTTTTT.jpg
 *   6    +    8    +    8   + ".jpg"
 * 定长字段是**故意**的：字典序排序就等于按 (boot, seq) 排序，
 * 也就是先进先出。如果改成变长（比如用 %lu 不补零），
 * "10" 会排在 "9" 前面，队列顺序就乱了。
 */
static void backlog_make_name(char *out, size_t cap, uint32_t boot_cnt,
                              uint32_t seq, int64_t uptime_s)
{
    snprintf(out, cap, "%06lu_%08lu_%08lu.jpg",
             (unsigned long)(boot_cnt % 1000000UL),
             (unsigned long)(seq % 100000000UL),
             (unsigned long)((uptime_s < 0 ? 0 : uptime_s) % 100000000LL));
}

/* 从文件名解析出采集时刻的 uptime（秒）。不是本模块的文件名就返回 -1。 */
static int64_t backlog_parse_uptime(const char *name)
{
    unsigned long boot = 0, seq = 0, up = 0;
    char tail[8] = {0};
    int consumed = 0;
    /*
     * 三段数字 + ".jpg"，并且**必须刚好用完整个名字**。
     *
     * 【为什么要用 %n 卡"刚好用完"】
     *   只检查 sscanf 返回 4 是不够的：%3s 读完 "jpg" 就停了，
     *   所以 "000001_00000001_00000001.jpg_extra" 这种
     *   "前缀像我们的名字、后面还挂着东西"的文件也会被判成合法。
     *   这种文件不是我们写的（我们只写定长 28 字符的名字），
     *   一旦混进队列，它的字典序可能永远最小 —— 于是每次补传都取它、
     *   每次都失败，整个队列**永远卡在第一帧**。
     *   %n 把已消费的字符数取回来，跟 strlen 一比就能排掉它。
     */
    if (sscanf(name, "%6lu_%8lu_%8lu.%3s%n",
               &boot, &seq, &up, tail, &consumed) != 4) {
        return -1;
    }
    if (consumed != (int)strlen(name)) {
        return -1;
    }
    if (strcmp(tail, "jpg") != 0) {
        return -1;
    }
    return (int64_t)up;
}

static bool backlog_is_ours(const char *name)
{
    return backlog_parse_uptime(name) >= 0;
}

/* ---------------- 目录扫描 ---------------- */

/*
 * 扫一遍队列目录，找出字典序最小的那个文件名（= 最老的一帧）。
 * 顺便统计条数。
 *
 * 【为什么每次都扫目录，不缓存一个有序列表】
 *   队列最多几十条，扫一遍的开销可以忽略；
 *   而缓存列表要处理"写失败/掉电/外部删除"导致的不一致。
 *   这里宁可慢一点，也不要多一份可能与磁盘对不上的状态。
 */
static esp_err_t backlog_find_oldest(char *out_name, size_t cap, int *out_count)
{
    DIR *dir = opendir(BACKLOG_MOUNT_POINT);
    if (dir == NULL) {
        return ESP_FAIL;
    }
    char oldest[BACKLOG_NAME_LEN] = {0};
    int count = 0;
    struct dirent *ent;
    while ((ent = readdir(dir)) != NULL) {
        if (!backlog_is_ours(ent->d_name)) {
            continue;                       /* 跳过 . / .. 和任何非本模块的文件 */
        }
        /*
         * 名字长度先卡一道。
         * backlog_is_ours() 已经保证了格式（定长 28 字符），但这层关系
         * 编译器看不出来 —— 它只知道 d_name 有 255 字节，会报
         * "strncpy output may be truncated"（-Werror 下直接编译失败）。
         * 显式判长度既消掉这个警告，本身也是真实的边界检查：
         * 不靠"上游验过了"来保证这里的缓冲区够用。
         */
        size_t nlen = strlen(ent->d_name);
        if (nlen >= sizeof(oldest)) {
            continue;                       /* 超长的名字不可能是我写的 */
        }
        count++;
        if (oldest[0] == '\0' || strcmp(ent->d_name, oldest) < 0) {
            memcpy(oldest, ent->d_name, nlen + 1);   /* 含结尾 '\0' */
        }
    }
    closedir(dir);
    if (out_count) {
        *out_count = count;
    }
    if (oldest[0] == '\0') {
        return ESP_ERR_NOT_FOUND;
    }
    /* 用 snprintf 而不是 strncpy：strncpy 在截断时不补 '\0'，
     * 调用方一旦忘了手动补就会读越界 —— 这类"约定俗成的规矩"不该靠记性维持。 */
    snprintf(out_name, cap, "%s", oldest);
    return ESP_OK;
}

static esp_err_t backlog_unlink(const char *name)
{
    char path[128];
    snprintf(path, sizeof(path), "%s/%s", BACKLOG_MOUNT_POINT, name);
    if (unlink(path) != 0) {
        return ESP_FAIL;
    }
    return ESP_OK;
}

/* ---------------- 对外接口 ---------------- */

esp_err_t backlog_init(void)
{
#if !BACKLOG_ENABLE
    /*
     * 编译时就把这个功能关掉了。
     * ★ 这里刻意**返回"不支持"而不是"成功"** ——
     *   如果返回成功，调用方会以为队列可用，断网时日志会显示
     *   "已缓存一帧"，而实际上什么都没存。那种"看起来在工作"的假象
     *   比直接说"这个功能被关了"难排查得多。
     */
    ESP_LOGW(TAG, "断网补传已在编译时关闭（BACKLOG_ENABLE=0）：断网期间的帧会被丢弃");
    return ESP_ERR_NOT_SUPPORTED;
#else
    if (s_ready) {
        return ESP_OK;
    }
    esp_vfs_spiffs_conf_t conf = {
        .base_path = BACKLOG_MOUNT_POINT,
        .partition_label = BACKLOG_PARTITION_LABEL,
        .max_files = 5,
        .format_if_mount_failed = true,   /* 首次上电分区是空的，必须允许格式化 */
    };
    esp_err_t err = esp_vfs_spiffs_register(&conf);
    if (err != ESP_OK) {
        /* 挂载失败不影响其它功能 —— 但调用方必须如实报告"本帧没存下"，
         * 不能因为队列不可用就说"已缓存"。 */
        ESP_LOGE(TAG, "挂载 %s 分区失败: %s（断网期间将无法缓存，会如实报告丢弃）",
                 BACKLOG_PARTITION_LABEL, esp_err_to_name(err));
        return err;
    }
    size_t total = 0, used = 0;
    if (esp_spiffs_info(BACKLOG_PARTITION_LABEL, &total, &used) == ESP_OK) {
        ESP_LOGI(TAG, "断网缓存已挂载：总 %u KB，已用 %u KB",
                 (unsigned)(total / 1024), (unsigned)(used / 1024));
    }
    int n = 0;
    char tmp[BACKLOG_NAME_LEN];
    if (backlog_find_oldest(tmp, sizeof(tmp), &n) == ESP_ERR_NOT_FOUND) {
        n = 0;
    }
    if (n > 0) {
        /* 上次开机断网期间攒下来的帧还在 —— 这是**正常**情况，不是异常。
         * 重启不该把没传出去的帧丢掉，那正是这个队列存在的意义。 */
        ESP_LOGW(TAG, "队列里有 %d 帧是上次开机遗留的，联网后会一并补传", n);
    }
    s_ready = true;
    return ESP_OK;
#endif /* BACKLOG_ENABLE */
}

bool backlog_ready(void)
{
    return s_ready;
}

int backlog_count(void)
{
    if (!s_ready) {
        return 0;
    }
    int n = 0;
    char tmp[BACKLOG_NAME_LEN];
    if (backlog_find_oldest(tmp, sizeof(tmp), &n) == ESP_ERR_NOT_FOUND) {
        return 0;
    }
    return n;
}

uint32_t backlog_dropped_total(void)
{
    return s_dropped;
}

esp_err_t backlog_put(const uint8_t *buf, size_t len,
                      uint32_t boot_cnt, int64_t capture_uptime_s)
{
    if (!s_ready || buf == NULL || len == 0) {
        return ESP_ERR_INVALID_STATE;
    }
    /* 单帧过大：直接拒，不删老帧。删了也放不下，只会白白丢数据。 */
    if (len > BACKLOG_MAX_FRAME_BYTES) {
        ESP_LOGW(TAG, "单帧 %u 字节超过上限 %d，不入队（本帧如实丢弃）",
                 (unsigned)len, BACKLOG_MAX_FRAME_BYTES);
        return ESP_ERR_INVALID_SIZE;
    }

    /* ① 条数软上限 */
    if (backlog_count() >= BACKLOG_MAX_FRAMES) {
        char victim[BACKLOG_NAME_LEN];
        if (backlog_find_oldest(victim, sizeof(victim), NULL) == ESP_OK &&
            backlog_unlink(victim) == ESP_OK) {
            s_dropped++;
            ESP_LOGW(TAG, "队列已达 %d 帧上限，丢弃最老的 %s（累计丢 %lu 帧）",
                     BACKLOG_MAX_FRAMES, victim, (unsigned long)s_dropped);
        }
    }

    /* ② 分区空间：先看还剩多少，不够就先腾地方。
     *    SPIFFS 有块开销，这里留 len 的 1/4 再加 4KB 余量。 */
    size_t total = 0, used = 0;
    if (esp_spiffs_info(BACKLOG_PARTITION_LABEL, &total, &used) == ESP_OK) {
        size_t need = len + len / 4 + 4096;
        int guard = 0;
        while (total > used && (total - used) < need && guard++ < BACKLOG_MAX_FRAMES) {
            char victim[BACKLOG_NAME_LEN];
            if (backlog_find_oldest(victim, sizeof(victim), NULL) != ESP_OK) {
                break;                      /* 队列空了还是不够，只能让写操作去报错 */
            }
            if (backlog_unlink(victim) != ESP_OK) {
                break;
            }
            s_dropped++;
            ESP_LOGW(TAG, "分区空间不足，丢弃最老的 %s 腾地方（累计丢 %lu 帧）",
                     victim, (unsigned long)s_dropped);
            if (esp_spiffs_info(BACKLOG_PARTITION_LABEL, &total, &used) != ESP_OK) {
                break;
            }
        }
    }

    char name[BACKLOG_NAME_LEN];
    backlog_make_name(name, sizeof(name), boot_cnt, ++s_next_seq, capture_uptime_s);
    char path[128];
    snprintf(path, sizeof(path), "%s/%s", BACKLOG_MOUNT_POINT, name);

    FILE *f = fopen(path, "wb");
    if (f == NULL) {
        /* 写不开（多半是空间还是不够）→ 再丢一个最老的，重试一次。
         * 只重试一次：一直重试会把主循环卡在这里，而丢数据的事实
         * 已经由 s_dropped 记下来了，不影响"如实上报"。 */
        char victim[BACKLOG_NAME_LEN];
        if (backlog_find_oldest(victim, sizeof(victim), NULL) == ESP_OK &&
            backlog_unlink(victim) == ESP_OK) {
            s_dropped++;
            f = fopen(path, "wb");
        }
    }
    if (f == NULL) {
        ESP_LOGE(TAG, "入队失败：%s 打不开（本帧如实丢弃）", path);
        return ESP_FAIL;
    }
    size_t written = fwrite(buf, 1, len, f);
    /* 必须 fclose 并检查返回值：SPIFFS 的元数据是在 close 时落盘的，
     * 只看 fwrite 的返回值会把"元数据没写下去"当成成功。 */
    int closed = fclose(f);
    if (written != len || closed != 0) {
        ESP_LOGE(TAG, "入队失败：写入 %u/%u 字节，close=%d（删除这个半成品）",
                 (unsigned)written, (unsigned)len, closed);
        backlog_unlink(name);               /* 半写的文件不能留在队列里 */
        return ESP_FAIL;
    }
    ESP_LOGI(TAG, "已缓存一帧 %s（%u 字节，队列共 %d 帧）",
             name, (unsigned)len, backlog_count());
    return ESP_OK;
}

esp_err_t backlog_peek_oldest(uint8_t *buf, size_t cap, size_t *out_len,
                              int64_t *out_capture_uptime_s,
                              char *out_name, size_t name_cap)
{
    if (!s_ready || buf == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    char name[BACKLOG_NAME_LEN];
    esp_err_t err = backlog_find_oldest(name, sizeof(name), NULL);
    if (err != ESP_OK) {
        return err;                         /* 队列为空 */
    }
    char path[128];
    snprintf(path, sizeof(path), "%s/%s", BACKLOG_MOUNT_POINT, name);
    struct stat st;
    if (stat(path, &st) != 0 || st.st_size <= 0) {
        /* 文件读不到或长度为 0：这是坏条目，删掉它并让调用方下一轮再来。
         * 不删的话它会永远卡在队首，整个补传都停了。 */
        ESP_LOGW(TAG, "队首 %s 读不到或长度为 0，删除这个坏条目", name);
        backlog_unlink(name);
        return ESP_ERR_INVALID_SIZE;
    }
    if ((size_t)st.st_size > cap) {
        /* 缓冲区装不下：不截断、不假装成功 —— 截断出来的 JPEG 是坏的，
         * 服务端会拒收，而我们会以为"补传成功了"。 */
        ESP_LOGE(TAG, "队首 %s 有 %d 字节，超过缓冲区 %u 字节（不截断，跳过）",
                 name, (int)st.st_size, (unsigned)cap);
        return ESP_ERR_INVALID_SIZE;
    }
    FILE *f = fopen(path, "rb");
    if (f == NULL) {
        return ESP_FAIL;
    }
    size_t got = fread(buf, 1, (size_t)st.st_size, f);
    fclose(f);
    if (got != (size_t)st.st_size) {
        ESP_LOGE(TAG, "读取 %s 只拿到 %u/%d 字节", name, (unsigned)got, (int)st.st_size);
        return ESP_FAIL;
    }
    if (out_len) {
        *out_len = got;
    }
    if (out_capture_uptime_s) {
        *out_capture_uptime_s = backlog_parse_uptime(name);
    }
    if (out_name && name_cap > 0) {
        strncpy(out_name, name, name_cap - 1);
        out_name[name_cap - 1] = '\0';
    }
    return ESP_OK;
}

esp_err_t backlog_drop_oldest(void)
{
    if (!s_ready) {
        return ESP_ERR_INVALID_STATE;
    }
    char name[BACKLOG_NAME_LEN];
    esp_err_t err = backlog_find_oldest(name, sizeof(name), NULL);
    if (err != ESP_OK) {
        return err;
    }
    return backlog_unlink(name);
}
