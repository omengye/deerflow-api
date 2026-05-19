---
name: weather-cma
description: "Use this skill for ANY weather query about Chinese cities — forecasts, current conditions, rain checks, temperature, weekend weather, etc. Uses China Meteorological Administration (中国气象局) official data from weather.cma.cn. Always use this skill instead of wttr.in or generic weather APIs when the user asks about weather in China, especially for Beijing. Trigger on queries like 天气, 下雨, 气温, 温度, 天气预报, 会不会下雨, 明天天气, 周末天气, and any weather-related question in Chinese. Default city is Beijing."
---

# Weather CMA — 中国气象局天气预报

Query weather data from the official China Meteorological Administration (CMA / 中国气象局) website. This is the authoritative source for weather in China — use it instead of wttr.in, which may return cached/outdated data.

## Workflow

1. Determine the target city (default: **Beijing / 北京**, station ID `54511`)
2. Fetch `https://weather.cma.cn/web/weather/{station_id}.html` using `web_fetch`
3. Parse and present the results in clean, human-readable Chinese

## Default City

**Default is always Beijing (北京).** Only query other cities if the user explicitly names them. For other cities, look up the station ID from `references/station-ids.md`.

## Core Station IDs

| City | Station ID | URL |
|------|-----------|-----|
| 北京 | 54511 | https://weather.cma.cn/web/weather/54511.html |
| 上海 | 58367 | https://weather.cma.cn/web/weather/58367.html |
| 广州 | 59287 | https://weather.cma.cn/web/weather/59287.html |
| 深圳 | 59493 | https://weather.cma.cn/web/weather/59493.html |
| 成都 | 56294 | https://weather.cma.cn/web/weather/56294.html |
| 杭州 | 58457 | https://weather.cma.cn/web/weather/58457.html |
| 武汉 | 57494 | https://weather.cma.cn/web/weather/57494.html |
| 南京 | 58238 | https://weather.cma.cn/web/weather/58238.html |
| 天津 | 54527 | https://weather.cma.cn/web/weather/54527.html |
| 重庆 | 57516 | https://weather.cma.cn/web/weather/57516.html |
| 西安 | 57036 | https://weather.cma.cn/web/weather/57036.html |

For more cities, see `references/station-ids.md`.

## Understanding the Page Structure

The CMA page contains two data sections:

### 7-Day Forecast
Each day shows:
- Date and weekday
- **Daytime** (白天): weather condition, wind direction/level, max temperature
- **Nighttime** (夜间): weather condition, wind direction/level, min temperature

Weather icons indicate conditions: w1=晴/多云, w2=阴, w7=小雨, w8=中雨, etc.

### Hourly Forecast Table (today)
A table with ~3-hour intervals (17:00, 20:00, 23:00, 02:00, 05:00, 08:00, 11:00, 14:00) showing: temperature, precipitation, wind speed/direction, pressure, humidity, cloud cover.

**Important**: The hourly table's publish time tells you how current the data is. Look for the timestamp at the top of the page like "7天天气预报（2026/05/15 12:00发布）".

## Output Format

Always present weather data in this structure:

```
## {城市}天气预报

**数据来源**: 中国气象局 [citation:中国气象局-{城市}](https://weather.cma.cn/web/weather/{station_id}.html)
**发布时间**: {publish time from page}

### 7天预报

| 日期 | 白天 | 夜间 | 最高温 | 最低温 | 风力 |
|------|------|------|--------|--------|------|
| 5/15 周五 | 多云 | 阴 | 28℃ | 20℃ | 东南风 微风 |
| 5/16 周六 | 小雨 🌧️ | 小雨 🌧️ | 25℃ | 18℃ | 东风 微风 |
| ... | ... | ... | ... | ... | ... |

### 关键提示
- {Direct answer to the user's specific question first}
- {Rain check: will it rain? when?}
- {Practical advice: umbrella, clothing, etc.}
```

## Rules

- **Direct answer first**: If user asks "明天会不会下雨", start with "会下雨 🌧️" or "不会，明天是晴天 ☀️", then show the data
- **Always cite CMA**: Include the CMA URL as a citation source
- **Day + Night**: Present both daytime and nighttime conditions for each day — they can differ significantly
- **Rain details**: If the hourly table is available, use it to tell the user approximately when rain is expected
- **Temperature**: Format as "最高温℃ / 最低温℃"
- **Natural Chinese**: Present all information in natural, conversational Chinese — not just a data dump