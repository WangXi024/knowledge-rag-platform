/** 聚合协议的纯函数：显式枚举模式，校验独立证据并去重 SSE/完成事件，不访问网络。 */
import type { AggregateEvidence, EvidenceMode } from './ui-types';

export function requestedEvidenceMode(value: unknown): EvidenceMode | null {
  return value === undefined || value === 'original' ? 'original' : value === 'community' ? 'community' : null;
}

/** 所有会被展示或用于回读的字段都先核验，旧历史没有该字段时返回空列表。 */
export function validAggregateEvidence(value: unknown): value is AggregateEvidence {
  if (!value || typeof value !== 'object') return false;
  const item = value as Record<string, unknown>;
  const stringFields = ['aggregate_id', 'knowledge_base_id', 'community_id', 'title', 'summary',
    'graph_profile_ref', 'generator_model_ref', 'strategy_ref'];
  return item.kind === 'community_aggregate' && item.derived === true && item.origin === 'gateway_generated'
    && typeof item.label === 'string' && /^A[1-9][0-9]*$/u.test(item.label)
    && stringFields.every((field) => typeof item[field] === 'string' && Boolean((item[field] as string).trim()))
    && ['graph_generation', 'content_revision', 'configuration_version'].every((field) => Number.isSafeInteger(item[field]) && (item[field] as number) >= 0)
    && Array.isArray(item.sources) && item.sources.length >= 2 && item.sources.length <= 16 && item.sources.every((source: unknown) => {
      if (!source || typeof source !== 'object') return false;
      const row = source as Record<string, unknown>;
      return ['knowledge_base_id', 'document_id', 'document_name', 'chunk_id', 'quote']
        .every((field) => typeof row[field] === 'string' && Boolean((row[field] as string).trim()))
        && row.knowledge_base_id === item.knowledge_base_id
        && (row.active_version_id === null || typeof row.active_version_id === 'string')
        && Number.isSafeInteger(row.document_revision) && (row.document_revision as number) >= 0
        && Array.isArray(row.positions);
    });
}

export function mergeAggregateEvidence(current: AggregateEvidence[], incoming: unknown): AggregateEvidence[] {
  const values = Array.isArray(incoming) ? incoming : [incoming];
  const result = new Map(current.map((item) => [item.aggregate_id, item]));
  for (const value of values) {
    if (!validAggregateEvidence(value)) throw new Error('聚合证据格式或来源无法核验');
    result.set(value.aggregate_id, value);
  }
  const items = [...result.values()];
  if (new Set(items.map((item) => item.label)).size !== items.length) throw new Error('聚合证据标签冲突');
  return items;
}

export function historicalAggregateEvidence(value: unknown): AggregateEvidence[] {
  return Array.isArray(value) ? value.filter(validAggregateEvidence) : [];
}
