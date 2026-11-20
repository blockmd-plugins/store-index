# Block App Store Index (`blockmd-plugin`)

自动采集 GitHub 上标记了 `blockmd-plugin` Topic 的公开仓库及 Release 资产，生成静态 `store-index.json`。

## 采集规则

- **官方认证插件（`verified.json`）**：当前仅 `blockmd-plugins/block-sync` 为官方插件，其余能力均以个人或社区名义发布；官方插件归档必须包含中文 `README.md` 与纯英文 `README.en.md` 以及无 Emoji 的双语 `manifest.json`，未通过将直接阻断采集。
- **保留 ID**：自动跳过已内置于 Block 的保留 ID（如 `block-diagnostics`）。
- **去重保护**：除 `verified.json` 绑定的官方插件外，多个仓库冲突使用同一 `id` 时会同时跳过。
- **系统权限兼容包**：新客户端从 `system_apps` 读取带系统权限的版本，旧客户端从 `apps` 读取可用的兼容版本。同步插件的兼容包固定为 `v1.3.44`，与当前版本分别检查归档、清单及双语文档。
