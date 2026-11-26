# asc-bark

把 App Store Connect **Analytics Reports API** 里最新一个共同事件日的汇总，推送到 [Bark](https://github.com/Finb/bark-server/blob/master/docs/API_V2.md)。

Docker 的默认部署是常驻服务：`docker compose up -d` 之后容器一直运行。同一个数据目录里，如果还没有 `serve` 开始过抓取的记录，进程会先做一次与 `run` 相同的抓取和推送，然后再按环境变量里的日程等待。这条记录写在抓取开始之前；这次成功或失败，进程都继续等。之后每次进程重新启动都只等到下一个时刻再抓取，不会因为容器重启又立刻抓一次。成功、没有新数据，或这一次失败，进程都不会退出。目标 App、密钥和 Bark 都在运行时配置，程序里没有写死任何 App。

`init`、`run`、`run --dry-run` 和 `test-notification` 仍是一次性命令，用来创建报表请求或手动试跑。如果以前用宿主机的 cron 或计划任务每次执行 `docker compose run --rm asc-bark run`，可以删掉那条任务，只保留容器运行。

源码：<https://github.com/feong/asc-bark>

```bash
git clone https://github.com/feong/asc-bark.git
```

官方说明：

- [Analytics Reports API 概览](https://developer.apple.com/help/app-store-connect-analytics/overview/analytics-reports-api/)
- [Downloading Analytics Reports](https://developer.apple.com/documentation/appstoreconnectapi/downloading-analytics-reports)
- [App Store Discovery and Engagement](https://developer.apple.com/documentation/analytics-reports/app-store-discovery-and-engagement)
- [App Store Downloads](https://developer.apple.com/documentation/analytics-reports/app-download)
- [Data Completeness and Corrections](https://developer.apple.com/documentation/analytics-reports/data-completeness-corrections)

不使用 Sales and Trends，也不读取 App Store Connect 网页。

## 指标

只用两份 **Standard**、**DAILY** 报表。`name` 按苹果文档的写法，是报表标题加 `Standard` 或 `Detailed` 后缀：

| 报表 `name` | `category` | 用途 |
| --- | --- | --- |
| `App Store Downloads Standard` | `COMMERCE` | 下载 |
| `App Store Discovery and Engagement Standard` | `APP_STORE_ENGAGEMENT` | 展示、产品页浏览 |

Detailed 是另一套隐私处理，不是同一批事件的补充行，因此不会并入，也不会在找不到 Standard 时拿来代替。周报和月报的 `Date` 是该周或该月的第一天，不会当成某一个事件日。

同一实例的多个 segment 是这一份实例的分片，会全部相加。不同实例之间，同一个事件日只保留 `processingDate` 更新的那一份，不把两份行加在一起。`processingDate` 相同则保留 `ONGOING`，仍相同则保留实例 id 较大的一份。`checksum` 只用来确认文件字节，不作为时间排序。

汇总的是报表里的 **Counts（事件次数）**。Discovery 报表里的 **Unique Counts 不会相加**：同一用户可以出现在多个地区、设备或来源行里。

| 推送字段 | 算法 | 不是 |
| --- | --- | --- |
| 首次下载 | `Download Type` 为 `First-time Download` 的 `Counts` 之和 | 不含更新、恢复，也不是去重人数 |
| 重新下载 | `Download Type` 为 `Redownload` 的 `Counts` 之和 | 同上 |
| 下载合计 | 上面两项都有行时才相加 | 缺任何一项则显示「不可用」，不把缺行当成 0 |
| 展示次数 | `Event` 为 `Impression` 的 `Counts` 之和 | 不含 Page view；不是 Unique Counts |
| 产品页浏览 | `Event` 为 `Page view` 且 `Page Type` 为 `Product page` 的 `Counts` 之和 | 不含 Store sheet、In-app event 等其它页面 |
| 转化率 | 固定为「不可用」 | 不用下载除以浏览代替 |

`Manual update`、`Auto-update`、`Restore` 以及未列出的下载类型都不进入下载数。`Pre-Order` 列不单独拆出，也不从 `Download Type` 里扣掉。维度行（地区、设备、版本、来源）会按 `Counts` 相加，因为每一行是一种组合下的事件次数。

某一类没有任何行时，该项是「不可用」。只有报表里真的出现了 `Counts` 为 0 的行，才显示 0。没有数据就不会推送，也不会把缺失的日期补成 0。

事件日是文件里的 `Date`。处理日是实例的 `processingDate`，也就是苹果生成这份实例的日期。推送里会同时写出这两个日期。App Analytics 的日期基准是 UTC。这里不把它们换算成 Asia/Shanghai，也不把处理日当成事件日。

选择的事件日，是两份日报都实际出现过的 `Date` 里最晚的一天。只在其中一份出现的日期不会使用。官方完整窗口是下载约 2 天、发现与互动约 3 天；程序不会用这个窗口宣布某一天已经最终完整。较新的实例如果改变了汇总数，同一事件日会再推送一次。汇总数没变则不重复推送。Bark 没有明确成功之前，不会把这一天记成已推送。

## 故意不做的事

**转化率。** App Store Connect 的说明是：用查看产品页的人数，对应下载和预订总数来计算转化率（[报表工具概览](https://developer.apple.com/help/app-store-connect/measure-app-performance/overview-of-reporting-tools/)）。人数对应 Unique Counts，不能把不同维度行的 Unique Counts 加总；预订在另一份报表。因此这里不计算转化率。

**90 日汇总。** ONGOING 实例生成后只保留 35 天。把多天的 Counts 相加也不是 90 日去重人数，缺日或被隐私处理拿掉的行会被漏计。

**把延迟或隐私隐藏解释成 0 或“已经完整”。** 低量行可能不出现。Detailed 报表还有阈值和噪声；本程序不使用 Detailed。

## 权限、等待和历史

- 第一次创建 Analytics Report 请求需要 **Admin**。请求已经存在之后，**Admin**、**Sales and Reports** 或 **Finance** 可以列出并下载。`init` 会创建请求，所以它使用的密钥需要 Admin。`run` 和 `serve` 只读。
- 第一次 ONGOING 请求后，第一份报表通常要 **1–2 天（约 24–48 小时）**。这段时间 `run` 会正常退出并说明还没有可下载的数据。`serve` 把同一句话写进日志，然后等到下一个日程。
- 按请求提供的历史从 **2024-01-01** 起。实例大约保留 **35** 天。更早的数据要用 `ONE_TIME_SNAPSHOT`。快照每月只能发起一次，而且不会在请求日之后继续产生新数据。本程序不删除苹果侧的请求。
- 长时间不下载时，ONGOING 会变成 `stoppedDueToInactivity`。再次执行 `init` 会另外创建一个新的 ONGOING，旧的停止请求仍留在账号里。

## 命令

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python3 asc_bark.py init
python3 asc_bark.py init --snapshot
python3 asc_bark.py run
python3 asc_bark.py run --dry-run
python3 asc_bark.py test-notification
python3 asc_bark.py serve
```

`init` 会幂等创建 `ONGOING`。加上 `--snapshot` 时，如果还没有 `ONE_TIME_SNAPSHOT` 才创建。`run` 只列出、下载和解析，然后退出。`--dry-run` 仍然读取苹果的报表，但不会调用 Bark，也不会把事件日记成已推送。`test-notification` 只发一条不含数据的 Bark 测试，必须单独执行，`run` 和 `serve` 都不会顺带发送。

`serve` 是容器的默认命令。它读取 `ASC_SCHEDULE` 和 `TZ`。`$ASC_DATA_DIR/serve.json` 里还没有次数大于 0 的运行记录时，启动后先执行一次与 `run` 相同的抓取，再睡到下一次日程。记录在这次抓取开始前写入。记录已经存在时，进程只睡到下一次日程，启动时不会立刻抓取。手动执行的 `run` 只更新 `state.json`，不会写入 `serve.json`，因此不算 `serve` 已经运行过。`run` 不看日程，适合手动试一次。

一次性命令：报表还在生成、最新实例还没有下载地址、两份日报没有共同事件日，或者共同事件日没有可汇总的行时，进程以状态码 0 退出并且不推送。配置错误是状态码 2。Bark 失败、校验失败或另一份进程正持有锁时是状态码 1；Bark 失败不会记成已推送，下次运行会再试。

`serve` 遇到上面这些运行结果只写日志，并等到下一个日程，不退出。第一次启动时立刻做的那一轮也一样：成功或失败都不会让进程退出。日程或时区无法解析，未来 8 年没有匹配的时刻，或还缺少密钥、私钥这类配置时，`serve` 在进入等待前以状态码 2 退出，也不会写下 `serve.json`。Docker 的重启策略会把它再拉起来，所以这类配置错误要改 `.env` 后再 `docker compose up -d`。收到 SIGTERM 或 SIGINT（包括 `docker stop` 和 Ctrl-C）时，如果正在等待就马上退出；如果这一轮抓取还在进行，就等它结束再退出，并且不会再开始下一轮。同一进程不会重叠执行两轮。另一份进程如果在抓取期间启动，仍会被数据目录上的 `asc-bark.lock` 挡住。

## 配置

见 `.env.example`。

| 变量 | 含义 |
| --- | --- |
| `ASC_ISSUER_ID` | App Store Connect API 的 Issuer ID |
| `ASC_KEY_ID` | 密钥 ID |
| `ASC_APP_ID` | 目标 App 的数字 Apple ID |
| `ASC_PRIVATE_KEY_PATH` | 只读挂载的 `.p8` 私钥 |
| `BARK_KEY` | Bark 设备密钥 |
| `BARK_SERVER` | Bark 服务器根地址，默认 `https://api.day.app` |
| `ASC_DATA_DIR` | 状态目录，容器内为 `/data` |
| `ASC_SCHEDULE` | 常驻日程。每天时刻 `HH:MM`，或五段 cron（分 时 日 月 周）。默认 `09:00`。只对 `serve` 生效 |
| `TZ` | 日程时区。默认 `Asia/Shanghai` |

`ASC_SCHEDULE` 默认 `09:00`，也就是 `TZ` 里的每天 09:00，等价于 cron `0 9 * * *`。五段 cron 例如 `15 8 * * 1-5`（周一到周五 08:15）。周几用数字，`0` 和 `7` 都是星期日，不支持英文星期名。日和周都写了具体值（都不是 `*`）时，满足任意一项就运行。本地时间不存在时（夏令时跳过的那一小时）会等到下一次真实存在的时刻，不会把已经错过的时刻补跑一遍。

这个钟点只决定何时去看报表，不能用来判断数据已经完整。到点后程序仍只采用两份日报里都已经出现的最新事件日。

`BARK_SERVER` 只写根地址，不要写入设备密钥、`/push` 或查询参数。推送使用 `POST {BARK_SERVER}/push`，JSON 里带 `device_key`。只有 HTTP 200 且响应 `code` 为 200 才算成功。

私钥用 ES256 签 JWT，`aud` 为 `appstoreconnect-v1`，有效期 15 分钟，短于苹果允许的 20 分钟。密钥、JWT 和带查询参数的签名下载地址不会写入状态文件；日志里的这类内容会被打码。

状态在 `$ASC_DATA_DIR/state.json`，同一目录用 `asc-bark.lock` 防止两份进程同时写。写入先落到临时文件再替换。状态里记录每个实例的 checksum 和按事件日汇总的 Counts；checksum 没变就不再下载。删除 `state.json` 后，下一次抓取会按当前汇总再推送一次，但不会因此把 `serve` 当成第一次启动。`/data` 应是本机目录，不要让多台机器同时写同一份状态。

`$ASC_DATA_DIR/serve.json` 只记录 `serve` 已经开始过的抓取：次数，以及第一次和最近一次是启动抓取（`startup`）还是到点抓取（`schedule`）。没有这个文件，或 `runs` 为 0，表示 `serve` 还没有开始过抓取。一次抓取在联系报表接口之前就把次数加上；抓取成功或失败都留下这条记录，所以进程重启不会因为刚启动再抓一次。文件无法读取或版本无法识别时不覆盖它，这次启动也不立刻抓取。删除 `serve.json` 后，下一次 `serve` 会再当作第一次启动并立刻抓取一次。手动执行的 `run`、`init` 和 `test-notification` 都不写这个文件。

分段会核对 `sizeInBytes`。官方示例里的 `checksum` 是 32 位十六进制，程序按文件字节的 MD5 核对；不一致就放弃这一次推送。

## Docker

镜像可以在本地构建，也可以拉取已发布的 `ghcr.io/feong/asc-bark`。已发布标签是一份多架构清单，包含 `linux/amd64` 和 `linux/arm64/v8`：`docker pull` 会按当前机器选择其中一份，不需要另写平台或另选标签。本地 `docker compose build` 只构建当前机器的架构。容器以 uid `10001` 运行，不监听端口，不挂载 Docker socket。私钥卷是只读的。`.env` 和 p8 不会被复制进镜像。默认命令是 `serve`，重启策略是 `unless-stopped`。`docker stop` 的宽限时间是 5 分钟，用来等正在进行的那一轮抓取结束；如果当时只是在等下一个日程，进程会在大约一秒内退出。

```bash
cp .env.example .env
mkdir -p data secrets
# 把 AuthKey_XXXX.p8 放到 secrets/AuthKey.p8
chown 10001:10001 data secrets/AuthKey.p8
chmod 700 data
chmod 400 secrets/AuthKey.p8
```

使用已发布镜像，在后台一直运行：

```bash
docker compose pull
docker compose run --rm --no-build asc-bark init
docker compose up -d --no-build
docker compose logs -f asc-bark
```

`ASC_IMAGE_TAG` 可以选择标签，默认 `latest`。例如 `ASC_IMAGE_TAG=v1.2.3 docker compose pull`。

在检出的源码里本地构建时，构建结果会标成同一个镜像名：

```bash
docker compose build
docker compose run --rm asc-bark init
docker compose up -d
```

一次性命令会覆盖默认的 `serve`，跑完即退出。已经在后台运行的服务继续按日程等待；只有两份进程正好同时抓取时，锁会让后启动的那一份失败并在日志里留下说明：

```bash
docker compose run --rm asc-bark run
docker compose run --rm asc-bark run --dry-run
docker compose run --rm asc-bark test-notification
```

日程写在 `.env` 的 `ASC_SCHEDULE` 和 `TZ`。改完后执行 `docker compose up -d`，让容器重新读取环境变量。

## 已发布镜像

推送到 `main` 时，GitHub Actions 用 Buildx 同时构建 `linux/amd64` 和 `linux/arm64/v8`，并发布成同一个 `ghcr.io/feong/asc-bark:latest` 清单。推送以 `v` 开头的版本标签（例如 `v1.2.3`）时，再发布同名标签，同样是这两份架构，不会改写 `latest`。拉取请求只构建这两份架构，不推送到仓库。工作流只用仓库自带的 `GITHUB_TOKEN`，不需要另外的注册表密钥。仓库是公开的时候，发布之后会把这个包设为公开，拉取不需要登录。

```bash
docker pull ghcr.io/feong/asc-bark:latest
docker run -d --name asc-bark --restart unless-stopped \
  --env-file .env \
  -v "$PWD/data:/data" \
  -v "$PWD/secrets/AuthKey.p8:/secrets/AuthKey.p8:ro" \
  ghcr.io/feong/asc-bark:latest
docker run --rm --env-file .env \
  -v "$PWD/data:/data" \
  -v "$PWD/secrets/AuthKey.p8:/secrets/AuthKey.p8:ro" \
  ghcr.io/feong/asc-bark:latest init
```

不写命令时就是 `serve`。上面的 `init` 只创建报表请求，不会留在后台。

## 测试

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python3 -m unittest discover -s tests -t . -v
```

测试只用合成的 TSV 和本地假 HTTP，不会访问苹果或 Bark。
