# RAG 框架与实施步骤

## 1. 当前范围

本阶段只搭建供应链证据 RAG 的接口、持久化元数据和适配器边界，不收集真实数据，不调用 Embedding、向量数据库、重排模型或 OCR。

已实现链路：

```text
Collection 配置
  -> Document 来源元数据登记
  -> 本地确定性 Chunk 预览（不持久化）
  -> Ingest / Embed / Index Job 计划（不执行）
  -> Retrieval Query 审计
  -> Hit / Citation 数据结构
  -> HTTP API 与 DSH 工作台 Bridge
```

当前默认适配器全部为 `none`。检索接口会持久化查询事实并返回 `status=not_configured`、空 `hits`、空 `citations` 和 `live_retrieval_verified=false`，不会用关键词匹配或随机分数伪装真实 RAG。

## 2. 代码结构

- `app/rag.py`：Provider Protocol、Null Adapter、分块预览和 RAGService。
- `app/store.py`：`rag_collections`、`rag_documents`、`rag_chunks`、`rag_jobs`、`rag_queries`、`rag_hits`。
- `app/api_contract.py`：RAG HTTP API 的唯一登记位置。
- `app/bridge.py`：DSH 可使用的 `rag.describe`、`rag.search`、`rag.citation`。
- `tests/test_rag.py`：无外部请求的框架合同测试。

## 3. HTTP 接口

| 方法 | 路径 | 当前行为 |
|---|---|---|
| GET | `/api/rag/capabilities` | 返回适配器和真实联调状态 |
| GET/POST | `/api/rag/collections` | 列出或创建 Collection 配置 |
| GET | `/api/rag/collections/{collection_id}` | 查看 Collection |
| GET/POST | `/api/rag/collections/{collection_id}/documents` | 列出或登记文档元数据 |
| GET | `/api/rag/documents/{document_id}` | 查看文档元数据 |
| GET | `/api/rag/documents/{document_id}/chunks` | 查看已入库 Chunk；当前为空 |
| GET/POST | `/api/rag/collections/{collection_id}/jobs` | 列出或创建计划任务 |
| GET | `/api/rag/jobs/{job_id}` | 查看任务计划及未执行原因 |
| POST | `/api/rag/documents/{document_id}/chunk-plan` | 预览字符分块，不落库 |
| POST | `/api/rag/collections/{collection_id}/query` | 查询；未配置时明确拒绝虚假召回 |
| GET | `/api/rag/queries/{query_id}` | 查询审计记录 |
| GET | `/api/rag/queries/{query_id}/citations` | 已持久化的引用证据；没有则 404 |

Collection、Document 和 Job 是本地元数据写入。`query` 被标记为 `external-read`，因为未来配置真实 Provider 后会调用外部检索组件；制作预览不会自动执行。

## 4. 数据与安全边界

- Document 接口只登记 `external_id`、来源引用、版本、checksum 和脱敏 metadata，不读取 `source_ref`。
- metadata 拒绝 token、secret、password、cookie、authorization、credential 和 API key 字段。
- 同一 Collection 下相同 `external_id + version` 必须保持 checksum 和 metadata 不变。
- `rag_chunks` 已建表，但当前 Chunk Preview 不写入；只有后续明确的采集/解析阶段才能入库。
- DSH Bridge 强制 Collection 与当前场景一致，物料会话不能跨场景检索交付或质量知识库。
- Citation 最少应绑定 query、document、document version、chunk、checksum、来源引用和召回方法；当前无真实 Hit，因此不生成 Citation。

## 5. 后续实施步骤

### 阶段 A：确定数据合同

1. 定义首批数据源清单，例如供应商协议、催交记录、质量报告和规则原文。
2. 为每种来源确定稳定 `external_id`、版本、更新时间和 checksum。
3. 定义脱敏规则、保留期限、场景权限和文档删除传播规则。
4. 制作一套只包含合成文档的离线评测问题、期望文档和禁止命中项。

### 阶段 B：解析与分块

1. 新增 Source Adapter，但只读取已授权的落盘产物或接口结果。
2. 使用结构化解析器处理 PDF、DOCX、表格和 OCR 结果。
3. 将字符预览 Chunker 替换为标题/段落/表格感知 Chunker。
4. 把文档版本、页码、章节和行列范围写入每个 Chunk 的 metadata。
5. 同一 checksum 重试保持幂等，旧版本不静默覆盖。

### 阶段 C：Embedding 与向量库

1. 实现一个 `EmbeddingProvider`，记录模型名、维度和配置版本。
2. 实现一个 `VectorStore`，建议首版使用 Qdrant 或 pgvector，避免同时支持多种后端。
3. 每次 Upsert 保存 `vector_ref`、provider、model 和 chunk checksum。
4. 批次失败保持 Job 为 failed/partial，不把空索引标成完成。
5. 建立 rebuild 与删除传播机制。

### 阶段 D：混合召回与重排

1. 增加关键词检索和向量检索的混合召回。
2. 统一候选分数，不直接比较不同算法的原始分值。
3. 接入可选 Reranker，并记录 rerank model 与分数。
4. 在检索前执行 scenario、部门、供应商和文档状态过滤。
5. 只把最终 Top-K 和完整 Citation 交给 DSH。

### 阶段 E：Agent 回答与评测

1. DSH 先调用 `rag.search`，再基于返回 Citation 回答。
2. `status` 不是 `completed` 或 citations 为空时，明确说明没有可引用证据。
3. 回答中显示来源标题、版本、章节/页码和时间。
4. 运行离线召回率、引用准确率、越权检索和无答案测试。
5. 只有真实 Provider、真实索引和评测通过后，才能设置 `live_retrieval_verified=true`。

## 6. 当前验收命令

```powershell
cd supply-chain-agent
.\.venv\Scripts\python.exe -m unittest tests.test_rag -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe scripts\export_api.py
.\.venv\Scripts\python.exe scripts\test_api.py --isolated
```

这些测试只证明框架、状态和审计合同可运行，不证明真实知识库、Embedding、向量检索或回答质量已经联调。
