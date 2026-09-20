# 配置字段、功能与验证对照

本表以用户修改后的 `src/redlotus/config.json` 为起点。数值不从 Python 或隐藏模板补齐；配置返回值保持独立副本。本表不含凭据。

| 字段 | 读取入口 | 对应功能／本轮处理 | 验证 |
|---|---|---|---|
| BASE_URL、API_KEY | core/config.get_env → gateway.ModelTarget | 模型连接；保持三层来源优先级 | config_resolution、gateway、真实请求 |
| SILICONFLOW_BASE、SILICONFLOW_KEY | memory/retrieval._rag_api_post | embedding/rerank 连接；不变 | rag、真实 RAG |
| RAG_models.embedding、reranker | memory/retrieval._require_rag_model | RAG 模型；不变 | optional_rag、rag |
| models.*.name | core/config.get_model_and_params | 各角色模型名称；不变 | model_selection、switching |
| models.*.max_tokens | core/gateway.create_model | 主执行参数不变；compressor 按批准值改为 131072 | 实际 HTTP body |
| models.*.temperature、top_p | core/config.apply_thinking_config | 原采样参数；不变 | gateway、实际 HTTP body |
| models.*.thinking、reasoning_effort | core/config.apply_thinking_config | 保持原配置，压缩模型 max | gateway、实际 HTTP body |
| models.*.auto_compress_ratio | core/history.compact_request_messages | 按批准值改为 0.9，只与实际输入用量比较 | config_compaction_contract、真实大上下文 |
| models.*.compress_head_turns、compress_tail_turns | core/history._compression_bounds | 保留首尾完整回合；不变 | context、long_turn_compression |
| models.*.max_context_windows（可选） | core/history.get_effective_max_context | 显式容量优先，否则 OpenRouter 元数据；不猜容量 | config_compaction_contract |
| conversation_log.* | core/session 会话写入及用量保留 | 单文件事务及统计；不变 | session、transaction、usage 系列 |
| lifecycle.* | core/agents、core/system、core/config.ExitDeadline | 生命周期与退出期限；不变 | lifecycle、session_thread_limit |
| short_term_memory.db_path、table_name | memory/retrieval.RAG、MemoryStore | LanceDB 实际位置与索引身份；不变 | memory_store、真实落盘 |
| short_term_memory.vector_search_limit、final_top_k、min_similarity | memory/retrieval.RAG.retrieve | 向量候选、过滤、最终条数；不变 | rag、真实召回 |
| short_term_memory.use_rerank | memory/retrieval.RAG.retrieve | 保留 rerank 开关 | rag、optional_rag |
| short_term_memory.turn_token_limit、turn_chunk_overlap_tokens | memory/retrieval.RAG._chunks | 超长正式记忆的 embedding 分块；不截断查询或感知证据 | rag 分块完整性 |
| short_term_memory.index.* | memory/retrieval.EmbedDataBase | LanceDB 向量索引加速；不变 | memory_store 索引重试 |
| long_term_memory.table_name | memory/store.MemoryStore | 全局 L2 向量空间；不变 | 项目隔离／全局消费 |
| request_limit | core/config.get_agent_usage_limits | 保持原请求上限／无限制含义 | gateway |
| MODEL_HTTP_TIMEOUT | core/gateway.ModelTarget | 保持实际请求超时 | gateway |
| memory_perception.window_turns、overlap_turns | memory/records.ObservationStore | 20 个新增真实回合、3 回合 overlap；不变 | perception_windows、60 轮真实验收 |
| memory_perception.model_role | memory/perception.target_for_job | 使用 Worker 生产记忆，与 compressor 解耦 | perception_runtime |
| memory_perception.input_context_ratio | 无有效读取（原预算裁剪已取消） | 已删除，不另设代码默认值 | 全量引用／证据检查 |
| memory_perception.existing_record_limit | 无有效读取 | 已删除；现有记忆由 Agent 检索 | memory_production_contract |
| input_limits.defaults.max_file_bytes、max_files、max_request_bytes | core/gateway.ModelInputPolicy | 保留已确认的附件和网关声明限制；与取消的正文裁剪不同 | references、input_budget |
| model_metadata.url、timeout | core/history._ensure_openrouter_maps | 现有元数据服务；不变 | metadata 获取及缺失容量提示 |
| model_metadata.supported_thinking_efforts | core/config.supported_thinking_efforts | CLI 思考选项；不变 | config、CLI 命令 |
| rag_service.timeout、http2 | memory/retrieval._get_shared_client | RAG 连接参数；不变 | rag |
| rag_service.embedding_batch_size、index_batch_size | memory/retrieval.embed_texts、store.reconcile | embedding 与索引批次；不变 | rag、索引恢复 |
| agent_run_policy.max_concurrent_threads_per_session | core/agents.SubagentFactory | 同 session 上限 16，取得名额才创建线程；不变 | session_thread_limit |
| agent_run_policy.max_command_timeout_seconds | tools/toolkit | 保留命令超时；不变 | execution、process_policy |
| storage.state_dir | core/config.user_data_dir | 全局记忆位置；不变 | storage、真实落盘 |
| storage.project_dir、sessions_dir、project_logs_dir | core/config 项目路径入口 | 项目 .redlotus 保存会话、感知状态和日志；不变 | session 加载／事务 |
| storage.references_dir、runtime_dir | core/config 项目路径入口 | 引用原件、依赖、缓存保留在项目 WorkDatabase；不变 | references、execution |
| storage.cleanup.* | core/config 保存失败清理 | 保留七天策略和保护条件；不变 | storage_failures |

## 废弃配置不再约束功能

- 旧 `context` 分组：不再参与容量和首尾回合读取；内部压缩字段不上模型 API。
- `query_max_chars`、`evidence_read_tokens`：不再裁剪查询和证据。
- `rag_service.query_instruction`：指令放入英文提示词资源，不从低优先级旧配置恢复。
- `agent_run_policy.max_tool_output_chars`：不再截断或以文件路径替代模型应收到的完整工具结果。
- `task_title.max_chars`：不再裁剪生成标题。

这些字段即使仍存在于用户旧全局 JSON，也不会恢复已取消的行为；本轮未擅自修改用户全局文件。

## 必要但尚待确认的字段

- 源码缺少的 `model_gateway`、`execution`：具体旧有效值及用途见 `config-compaction-memory-config-review.md`。等待确认后显式保留；当前真实请求仍使用既有配置合并结果，不能据此宣称源码单份配置已完整。
- `memory_perception.outcomes` 与 `default_outcome`：等待确认字段及值后，替代五种结果分类的代码枚举；不会改变成功、失败、取消、需补充和未验证的语义。

本表中的验证名称说明覆盖关系，不代表这些测试已经全部通过；实际结果另见进度和验收报告。
