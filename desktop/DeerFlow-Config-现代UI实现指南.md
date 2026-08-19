# DeerFlow ACP Config — 现代化 UI 实现指南（iced / Rust）

> 本文档描述如何用 [iced](https://iced.rs) 框架实现演示图中的深色现代化配置界面：
> 深海军蓝底色、青色品牌色、纯色扁平按钮、Status Pills 顶栏、大数字统计卡片、
> **Windows 风格自定义标题栏**。
>
> 适用版本：**iced 0.13.x**

---

## 目录

1. [设计规范（Design Tokens）](#1-设计规范design-tokens)
2. [项目结构与依赖](#2-项目结构与依赖)
3. [主题系统 theme.rs](#3-主题系统-themers)
4. [Windows 风格自定义标题栏](#4-windows-风格自定义标题栏)
5. [侧边栏实现](#5-侧边栏实现)
6. [顶部状态 Pills](#6-顶部状态-pills)
7. [统计卡片](#7-统计卡片)
8. [按钮系统（纯色扁平）](#8-按钮系统纯色扁平)
9. [微动效](#9-微动效)
10. [主入口 main.rs 组装](#10-主入口-mainrs-组装)
11. [常见坑](#11-常见坑)

---

## 1. 设计规范（Design Tokens）

所有视觉决策集中为一份 token 表，代码中禁止写裸颜色值。

### 1.1 颜色

| Token | 值 | 用途 |
|---|---|---|
| `BG_BASE` | `#0B1120` | 窗口最底层背景 |
| `BG_SURFACE` | `#111A2E` | 卡片、侧边栏背景 |
| `BG_ELEVATED` | `#1A2440` | 悬浮元素、hover 态 |
| `BORDER` | `#243150` | 卡片/按钮描边 |
| `TEXT_PRIMARY` | `#F1F5F9` | 主文字 |
| `TEXT_SECONDARY` | `#94A3B8` | 次要文字（slate-400） |
| `TEXT_MUTED` | `#64748B` | 弱化文字 |
| `ACCENT` | `#06B6D4` | 品牌青（cyan-500）|
| `SUCCESS` | `#10B981` | 运行中 / 启动按钮 |
| `WARNING` | `#F59E0B` | 重启按钮描边 |
| `DANGER` | `#EF4444` | 停止按钮描边 |
| `DANGER_TITLEBAR` | `#C42B1C` | Windows 关闭按钮 hover 专用红 |

> **注意**：按钮一律**纯色扁平**，不使用渐变。品牌感通过颜色语义而非渐变表达。

### 1.2 尺寸与圆角

| Token | 值 |
|---|---|
| 圆角-卡片 | `12px`（iced 中 `border::radius(12)`）|
| 圆角-按钮/Pill | `8px` |
| 内边距-卡片 | `20px` |
| 内边距-按钮 | 水平 `16px`，垂直 `8px` |
| 侧边栏宽度 | `220px` |
| 标题栏高度 | `36px` |
| 统计数字字号 | `36px Bold` |
| 正文字号 | `14px`，辅助 `12px` |

---

## 2. 项目结构与依赖

```
deerflow-config/
├── Cargo.toml
└── src/
    ├── main.rs          # 入口 + Application
    ├── theme.rs         # 颜色 token + 通用样式函数
    ├── titlebar.rs      # Windows 风格自定义标题栏
    ├── sidebar.rs       # 侧边栏
    ├── pills.rs         # 状态 Pill 组件
    ├── cards.rs         # 统计卡片
    └── views/
        └── overview.rs  # 概览页
```

```toml
# Cargo.toml
[dependencies]
iced = { version = "0.13", features = ["tokio", "svg", "image"] }
```

> 不需要 `iced_aw` 也能实现全部效果；组件全部自绘，风格更可控。

---

## 3. 主题系统 theme.rs

```rust
use iced::border::Radius;
use iced::{
    border, Border, Color, Theme,
    widget::{container, text, Text},
};

// ---------- 颜色 Token ----------
pub const BG_BASE: Color = color(0x0B, 0x11, 0x20);
pub const BG_SURFACE: Color = color(0x11, 0x1A, 0x2E);
pub const BG_ELEVATED: Color = color(0x1A, 0x24, 0x40);
pub const BORDER: Color = color(0x24, 0x31, 0x50);
pub const TEXT_PRIMARY: Color = color(0xF1, 0xF5, 0xF9);
pub const TEXT_SECONDARY: Color = color(0x94, 0xA3, 0xB8);
pub const TEXT_MUTED: Color = color(0x64, 0x74, 0x8B);
pub const ACCENT: Color = color(0x06, 0xB6, 0xD4);
pub const SUCCESS: Color = color(0x10, 0xB9, 0x81);
pub const WARNING: Color = color(0xF5, 0x9E, 0x0B);
pub const DANGER: Color = color(0xEF, 0x44, 0x44);
pub const DANGER_TITLEBAR: Color = color(0xC4, 0x2B, 0x1C);

const fn color(r: u8, g: u8, b: u8) -> Color {
    Color::from_rgb(r as f32 / 255.0, g as f32 / 255.0, b as f32 / 255.0)
}

// ---------- 通用卡片样式 ----------
pub fn card_style(theme: &Theme) -> container::Style {
    container::Style {
        background: BG_SURFACE.into(),
        border: Border {
            color: BORDER,
            width: 1.0,
            radius: Radius::from(12),
        },
        ..Default::default()
    }
}

// hover 变亮的卡片（配合 mouse_area 使用）
pub fn card_hover_style(_theme: &Theme) -> container::Style {
    container::Style {
        background: BG_ELEVATED.into(),
        border: Border {
            color: ACCENT,          // 边框变品牌色
            width: 1.0,
            radius: Radius::from(12),
        },
        ..Default::default()
    }
}

// ---------- 文字快捷函数 ----------
pub fn heading(t: &str, size: f32) -> Text<'static> {
    text(t.to_string()).size(size).color(TEXT_PRIMARY)
}

pub fn secondary(t: &str) -> Text<'static> {
    text(t.to_string()).size(12).color(TEXT_SECONDARY)
}
```

**要点**：
- 颜色全部 `const`，编译期检查，改主题只改一处。
- `card_style` / `card_hover_style` 成对出现，配合 `mouse_area` 实现 hover 切换。

---

## 4. Windows 风格自定义标题栏

Windows 应用的右上角应为 **最小化 `—`、最大化 `□`、关闭 `×`** 三个矩形按钮，
关闭按钮 hover 时变红（`#C42B1C`）。iced 默认使用系统装饰，需要关掉后自绘。

### 4.1 关闭系统装饰

```rust
// main.rs 中
fn main() -> iced::Result {
    deerflow_config::Application::run(Settings {
        window: window::Settings {
            decorations: false,     // 关键：去掉系统标题栏
            size: Size::new(1280.0, 800.0),
            ..Default::default()
        },
        ..Default::default()
    })
}
```

### 4.2 标题栏实现 titlebar.rs

```rust
use crate::theme::*;
use iced::{
    alignment, border, Border, Length,
    widget::{button, container, row, text, Row},
    Element,
};

#[derive(Clone, Debug)]
pub enum TitlebarMessage {
    Minimize,
    ToggleMaximize,
    Close,
}

pub fn view<'a>(title: &str) -> Element<'a, TitlebarMessage> {
    let caption = text(title.to_string())
        .size(12)
        .color(TEXT_SECONDARY);

    Row::new()
        .spacing(8)
        .align_y(alignment::Alignment::Center)
        .push(
            container(caption)
                .padding([0, 12])
                .width(Length::Fill)
        )
        .push(win_btn("—", TitlebarMessage::Minimize, false))
        .push(win_btn("□", TitlebarMessage::ToggleMaximize, false))
        .push(win_btn("✕", TitlebarMessage::Close, true)) // 关闭=危险色
        .height(36)
        .width(Length::Fill)
        .into()
}

/// Windows 风格矩形按钮：无圆角、无底色，hover 才有底色
fn win_btn<'a>(glyph: &str, msg: TitlebarMessage, is_close: bool) -> Element<'a, TitlebarMessage> {
    button(
        text(glyph.to_string())
            .size(14)
            .color(TEXT_SECONDARY)
            .align_x(alignment::Alignment::Center)
            .align_y(alignment::Alignment::Center),
    )
    .width(46)
    .height(36)
    .padding(0)
    .style(move |theme, status| {
        let hovered = matches!(status, button::Status::Hovered);
        button::Style {
            background: if hovered {
                // 关闭按钮 hover 用 Windows 专用红，其余用浅色
                (if is_close { DANGER_TITLEBAR } else { BG_ELEVATED }).into()
            } else {
                Color::TRANSPARENT.into()
            },
            text_color: if hovered && is_close {
                Color::WHITE
            } else {
                TEXT_SECONDARY
            },
            border: Border {
                radius: border::radius(0),   // Windows 按钮 = 直角
                ..Default::default()
            },
            ..Default::default()
        }
    })
    .on_press(msg)
    .into()
}
```

### 4.3 处理窗口消息（iced 0.13）

```rust
// Application::update 中
Message::Titlebar(TitlebarMessage::Minimize) => {
    if let Some(handle) = self.window.clone() {
        let _ = handle.minimize(true);
    }
}
Message::Titlebar(TitlebarMessage::ToggleMaximize) => {
    if let Some(handle) = self.window.clone() {
        let _ = handle.toggle_maximize();
    }
}
Message::Titlebar(TitlebarMessage::Close) => {
    window::close(self.window.clone().unwrap());
    return Task::none();
}

// subscription 中获取窗口句柄
fn subscription(&self) -> Subscription<Message> {
    window::id().map(|id| Message::WindowReady(id))  // 保存 id
}
```

> **拖拽移动窗口**：iced 0.13 中给标题栏空白区域包一层
> `mouse_area(...).on_drag(Message::DragWindow)`，配合 window 句柄的
> `drag()` 方法即可实现按住标题栏拖动。

---

## 5. 侧边栏实现

侧边栏 = 固定宽度 `container` + 纵向 `column` 的导航项。
**active 项的关键视觉**：左侧 3px 青色竖条 + 半透明青色背景。

```rust
use crate::theme::*;
use iced::{
    alignment, border, Border, Length,
    widget::{button, column, container, row, text},
    Element,
};

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Page {
    Overview, Models, Agents, Skills,
    Sandbox, Runtime, Diagnostics,
}

pub fn view<'a>(active: Page) -> Element<'a, Page> {
    let items = [
        ("◆", "概览", Page::Overview),
        ("▢", "模型", Page::Models),
        ("bots", "Agent", Page::Agents),
        ("✦", "Skills", Page::Skills),
        ("🔧", "Sandbox / Tools", Page::Sandbox),
        ("⚙", "Runtime / ACP", Page::Runtime),
        ("🩺", "诊断", Page::Diagnostics),
    ];

    let mut list = column().spacing(4);

    for (icon, label, page) in items {
        let is_active = page == active;

        let content = row!()
            .spacing(10)
            .align_y(alignment::Alignment::Center)
            .push(text(icon.to_string()).size(14).color(
                if is_active { ACCENT } else { TEXT_MUTED }
            ))
            .push(text(label.to_string()).size(14).color(
                if is_active { TEXT_PRIMARY } else { TEXT_SECONDARY }
            ))
            .padding([8, 12]);

        let item = button(content)
            .width(Length::Fill)
            .style(move |theme, status| {
                let hovered = matches!(status, button::Status::Hovered);
                button::Style {
                    background: if is_active {
                        // 半透明青色底：ACCENT 带 12% 透明度
                        Color { a: 0.12, ..ACCENT }.into()
                    } else if hovered {
                        BG_ELEVATED.into()
                    } else {
                        Color::TRANSPARENT.into()
                    },
                    border: Border {
                        color: if is_active { ACCENT } else { Color::TRANSPARENT },
                        width: if is_active { 0.0 } else { 0.0 },
                        radius: border::radius(6),
                    },
                    ..Default::default()
                }
            })
            .on_press(page);

        // active 项左侧叠加 3px 竖条
        let wrapped = if is_active {
            row!()
                .spacing(8)
                .push(
                    container(iced::widget::Space::new(3, 20))
                        .style(|_| container::Style {
                            background: ACCENT.into(),
                            border: Border { radius: border::radius(2), ..Default::default() },
                            ..Default::default()
                        })
                )
                .push(item)
                .into()
        } else {
            row!().push(item).into()
        };

        list = list.push(wrapped);
    }

    container(
        column![]
            .spacing(4)
            .push(
                column![]
                    .push(text("DEERFLOW").size(11).color(TEXT_MUTED))
                    .push(text("ACP CONFIG").size(11).color(TEXT_MUTED))
                    .padding([0, 12, 16, 12])
            )
            .push(list)
    )
    .padding([16, 12])
    .width(220)
    .height(Length::Fill)
    .style(|_| container::Style {
        background: BG_SURFACE.into(),
        border: Border {
            color: BORDER,
            width: 0.0,
            radius: border::radius(0),
            ..Default::default()
        },
        ..Default::default()
    })
    .into()
}
```

**要点**：
- active 底色用 `Color { a: 0.12, ..ACCENT }` 半透明叠加，比纯色块更精致。
- 图标在生产环境建议换成 SVG（iced `widget::svg`）或 icon font，emoji 跨平台渲染不一致。
- 侧边栏与主内容之间用 1px `BORDER` 分隔（右侧加竖线 `container`）。

---

## 6. 顶部状态 Pills

每个指标独立成一个圆角胶囊，状态灯用小圆点 + 成功色。

```rust
use crate::theme::*;
use iced::{border, Border, Length};
use iced::widget::{container, row, text};

/// 状态指示灯：8px 圆点
fn dot(color: iced::Color) -> iced::widget::Space {
    iced::widget::Space::new(8, 8)
}

fn dot_container(color: iced::Color) -> container::Container<'static, ()> {
    container(iced::widget::Space::new(8, 8)).style(move |_| container::Style {
        background: color.into(),
        border: Border {
            radius: border::radius(4),  // 正圆
            ..Default::default()
        },
        ..Default::default()
    })
}

pub fn pill(label: &str, dot_color: Option<iced::Color>) -> container::Container<'static, ()> {
    let mut content = row!().spacing(6).align_y(iced::alignment::Alignment::Center);
    if let Some(c) = dot_color {
        content = content.push(dot_container(c));
    }
    content = content.push(text(label.to_string()).size(12).color(TEXT_SECONDARY));

    container(content)
        .padding([6, 12])
        .style(|_| container::Style {
            background: BG_SURFACE.into(),
            border: Border {
                color: BORDER,
                width: 1.0,
                radius: border::radius(999), // 全圆角=胶囊
            },
            ..Default::default()
        })
}
```

**组合效果**：

```rust
row!()
    .spacing(8)
    .push(pill("Running", Some(SUCCESS)))
    .push(pill("PID 24412", None))
    .push(pill("v0.0.4-dev9", None))
    .push(pill("localhost:54283", None))
    .push(iced::widget::Space::with_width(Length::Fill))
    .push(/* Secondary 按钮：刷新 */)
    .push(/* Primary 按钮：保存配置 */)
    .padding([0, 24, 16, 24])
```

---

## 7. 统计卡片

**核心**：小图标 + 标签在顶部，36px 大号品牌色数字，辅助说明在底部。
外层用 `mouse_area` 实现 hover 边框变亮。

```rust
use crate::theme::*;
use iced::{
    alignment, border, Border, Length,
    widget::{column, container, mouse_area, row, text},
    Element,
};
use iced::widget::mouse_area::Event::*;

#[derive(Clone, Debug)]
pub struct StatCard {
    pub icon: String,     // 生产环境换 SVG
    pub value: String,    // "22/22"
    pub label: String,    // "Skills"
    pub hint: String,     // "已加载 22 个技能"
}

pub fn stat_card<'a>(card: &StatCard, hovered: bool) -> Element<'a, ()> {
    let body = column![]
        .spacing(8)
        .push(
            row!()
                .spacing(8)
                .push(text(card.icon.clone()).size(14).color(ACCENT))
                .push(text(card.label.clone()).size(13).color(TEXT_SECONDARY))
        )
        .push(
            text(card.value.clone())
                .size(36)
                .color(TEXT_PRIMARY)   // 数字用主色；如需强调可换 ACCENT
                .font(iced::Font {
                    family: iced::font::Family::Default,
                    weight: iced::font::Weight::Bold,
                    ..Default::default()
                })
        )
        .push(text(card.hint.clone()).size(12).color(TEXT_MUTED))
        .padding(20);

    let styled = container(body).style(move |_| {
        // hover 时切换到 card_hover_style 的等效样式
        container::Style {
            background: if hovered { BG_ELEVATED } else { BG_SURFACE }.into(),
            border: Border {
                color: if hovered { ACCENT } else { BORDER },
                width: 1.0,
                radius: border::radius(12),
            },
            ..Default::default()
        }
    });

    mouse_area(styled)
        .on_enter(())
        .on_exit(())
        .into()
}
```

四张卡片用 `row![].spacing(16)` + 每张 `width(Length::Fill)` 均分。

---

## 8. 按钮系统（纯色扁平）

定义四类按钮样式函数，全部**纯色**、无渐变：

```rust
use crate::theme::*;
use iced::{border, Border, Color};

pub fn primary_btn(theme: &iced::Theme, status: iced::widget::button::Status)
    -> iced::widget::button::Style
{
    let hovered = matches!(status, iced::widget::button::Status::Hovered);
    let pressed = matches!(status, iced::widget::button::Status::Pressed);
    iced::widget::button::Style {
        // 主按钮：青色实底，hover 提亮，按下压暗
        background: if pressed {
            Color { a: 0.8, ..ACCENT }.into()
        } else if hovered {
            Color::from_rgb(0x22, 0xC5, 0xE0).into()   // 提亮 10%
        } else {
            ACCENT.into()
        },
        text_color: iced::Color::WHITE,
        border: Border {
            radius: border::radius(8),
            ..Default::default()
        },
        ..Default::default()
    }
}

pub fn secondary_btn(theme: &iced::Theme, status: iced::widget::button::Status)
    -> iced::widget::button::Style
{
    let hovered = matches!(status, iced::widget::button::Status::Hovered);
    iced::widget::button::Style {
        background: if hovered { BG_ELEVATED.into() } else { Color::TRANSPARENT.into() },
        text_color: ACCENT,
        border: Border {
            color: if hovered { ACCENT } else { BORDER },
            width: 1.0,
            radius: border::radius(8),
        },
        ..Default::default()
    }
}

/// 语义描边按钮（危险=红 / 警告=橙），用于停止/重启
pub fn outline_semantic(color: Color) -> impl Fn(&iced::Theme, iced::widget::button::Status)
    -> iced::widget::button::Style
{
    move |_theme, status| {
        let hovered = matches!(status, iced::widget::button::Status::Hovered);
        iced::widget::button::Style {
            background: if hovered { Color { a: 0.15, ..color }.into() }
                        else { Color::TRANSPARENT.into() },
            text_color: color,
            border: Border {
                color,
                width: 1.0,
                radius: border::radius(8),
            },
            ..Default::default()
        }
    }
}
```

**Daemon 控制区用法**：

```rust
row!()
    .spacing(12)
    .push(button("启动 Daemon").style(primary_btn))                          // 语义上也可用绿实底
    .push(button("停止 Daemon").style(outline_semantic(DANGER)))   // 红描边
    .push(button("重启 Daemon").style(outline_semantic(WARNING))) // 橙描边
```

> 「启动」如需与保存按钮区分，可增加 `success_solid_btn`：`SUCCESS` 实底白字，
> 样式函数与 `primary_btn` 同构，仅换颜色 token。

---

## 9. 微动效

### 9.1 状态灯呼吸（Subscription 驱动）

```rust
#[derive(Clone, Debug)]
pub enum Message {
    Tick(iced::time::Instant),
}

// subscription：每 50ms 一次
fn subscription(&self) -> Subscription<Message> {
    iced::time::every(std::time::Duration::from_millis(50))
        .map(Message::Tick)
}

// update 中计算呼吸相位
Message::Tick(now) => {
    let t = now.elapsed().as_secs_f32();       // 按需改用自存起始时间
    self.glow = (t * std::f32::consts::PI).sin() * 0.5 + 0.5; // 0.0~1.0
}

// 渲染时插值
let dot_color = Color {
    a: 0.5 + self.glow * 0.5,   // 透明度 50%~100% 呼吸
    ..SUCCESS
};
```

### 9.2 数字加载动画

进入页面时从 0 数到目标值（每 tick 前进 10%）：

```rust
Message::Tick(_) => {
    if self.displayed < self.target {
        self.displayed = (self.displayed + (self.target - self.displayed) * 0.2 + 1.0)
            .min(self.target);
    }
}
```

### 9.3 可选：简化方案

呼吸灯、数字动画都基于同一条 `time::every` 订阅，成本极低。
如果不需要动画，删除 subscription 即可，不影响布局。

---

## 10. 主入口 main.rs 组装

```rust
mod theme;
mod titlebar;
mod sidebar;
mod pills;
mod cards;
mod views;

use iced::{Alignment, Element, Length, Settings, Size, Subscription, Task, Theme as IcedTheme};
use iced::widget::{column, container, row, text};

fn main() -> iced::Result {
    App::run(Settings {
        window: iced::window::Settings {
            decorations: false,               // 自绘 Windows 标题栏
            size: Size::new(1280.0, 800.0),
            ..Default::default()
        },
        default_font: None,
        ..Default::default()
    })
}

#[derive(Clone, Debug)]
pub enum Message {
    Titlebar(titlebar::TitlebarMessage),
    Page(sidebar::Page),
    Tick(iced::time::Instant),
}

pub struct App {
    page: sidebar::Page,
    window: Option<iced::window::Id>,
    glow: f32,
}

impl iced::Application for App {
    type Message = Message;
    type Theme = IcedTheme;
    type Executor = iced::executor::Default;
    type Flags = ();

    fn new(_flags: ()) -> (Self, Task<Message>) {
        (Self { page: sidebar::Page::Overview, window: None, glow: 0.0 }, Task::none())
    }

    fn title(&self) -> String { "DeerFlow ACP Config".into() }

    fn update(&mut self, message: Message) -> Task<Message> {
        match message {
            Message::Titlebar(titlebar::TitlebarMessage::Close) => {
                if let Some(id) = self.window {
                    return iced::window::close(id);
                }
            }
            // ... 其余窗口操作 / 页面切换 / Tick
            _ => {}
        }
        Task::none()
    }

    fn subscription(&self) -> Subscription<Message> {
        iced::time::every(std::time::Duration::from_millis(50)).map(Message::Tick)
    }

    fn view(&self) -> Element<Message> {
        // 全局深色底 + 布局：标题栏 / (侧边栏 + 主内容)
        container(
            column![]
                .push(titlebar::view("DeerFlow ACP Config").map(Message::Titlebar))
                .push(
                    row![]
                        .push(sidebar::view(self.page).map(Message::Page))
                        .push(main_content(self.page))
                )
        )
        .width(Length::Fill)
        .height(Length::Fill)
        .style(|_| container::Style {
            background: theme::BG_BASE.into(),
            ..Default::default()
        })
        .into()
    }

    fn theme(&self) -> IcedTheme { IcedTheme::Dark } // 实际颜色由 style 覆盖
}
```

---

## 11. 常见坑

| 坑 | 说明 / 解决 |
|---|---|
| **系统字体发虚** | Windows 下默认字体渲染小字号偏虚，`text` 的 `size` 不要小于 11；中文建议 `Settings::default_font` 指定 `Microsoft YaHei UI` 或打包 `HarmonyOS Sans` |
| **emoji 图标不一致** | Windows 的 emoji 是彩色的，与线性图标混排很丑；生产用 `widget::svg` 加载 [lucide](https://lucide.dev) 静态 SVG |
| **decorations=false 后无法拖动/缩放** | 必须自实现：标题栏 `mouse_area` 的 `on_drag` 调 `window::drag`，边缘 `on_resize` 调 `window::resize`；嫌麻烦可保留系统装饰，只做内部 UI |
| **窗口句柄获取** | iced 0.13 通过 `window::id()` subscription 或 `window::get_latest()` 拿 `Id`，再配合 `window::Action` 发指令 |
| **hover 状态需要自管理** | `container` 原生无 hover；用 `mouse_area` 包裹，`on_enter/on_exit` 发消息切换 bool，重算样式 |
| **Color 透明度** | `Color::TRANSPARENT` 是 `a:0`；半透明色用 `Color { a: 0.12, ..ACCENT }` 结构体更新语法 |
| **性能** | `time::every(50ms)` 只更新轻量字段（相位/计数），不要在 update 里做 IO |

---

## 附：实现优先级建议

1. **第一步**：theme.rs token 化 + 卡片/Pill/按钮三套样式 → 立刻获得 80% 视觉提升
2. **第二步**：侧边栏 active 高亮 + 图标（SVG）
3. **第三步**：自绘 Windows 标题栏（含关闭红、拖拽）
4. **第四步**：呼吸灯与数字动画等微动效
