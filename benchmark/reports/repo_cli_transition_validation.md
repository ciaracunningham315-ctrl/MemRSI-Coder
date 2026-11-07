# repo_cli T3 结构变化核对

本报告记录离线文件、符号和哈希证据；没有真实 Agent 历史轨迹、模型调用或正式结果。

检查结果：通过。

| 检查 | 结果 |
|---|---:|
| `old_config_helper_exists_in_t2_reference` | `True` |
| `old_command_helper_exists_in_t2_reference` | `True` |
| `old_config_helper_absent_in_t3` | `True` |
| `old_command_helper_absent_in_t3` | `True` |
| `new_settings_helper_exists_in_t3` | `True` |
| `legacy_ini_format_documented_in_t2` | `True` |
| `json_snapshot_format_documented_in_t3` | `True` |
| `old_resolver_symbols_present_in_t2` | `{'ConfigResolver': True, 'resolve': True, 'mirror_workers': True}` |
| `old_resolver_symbols_absent_in_t3` | `{'ConfigResolver': True, 'resolve': True, 'mirror_workers': True}` |
| `replacement_symbol_load_options_present` | `True` |
| `validation_helper_path_retained` | `True` |
| `bounded_decimal_retained` | `True` |
| `retained_helper_content_unchanged` | `True` |

T2 历史代码中的 `cli.validation.bounded_decimal` 与 T3 内容哈希相同，保留参数边界检查经验；旧配置入口与 INI 表示已移除，T3 使用新的 JSON snapshot loader。
实际 Agent 轨迹尚未采集，故这里的 stale/valid 区分仅是可复核的代码结构证据。
