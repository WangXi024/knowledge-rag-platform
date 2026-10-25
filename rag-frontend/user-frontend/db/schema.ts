/**
 * Drizzle 静态 Schema 当前留空：开发联调身份不落账号表；对话历史表由服务端模块用
 * ``CREATE TABLE IF NOT EXISTS`` 幂等创建，因此这里不重复声明可能漂移的模型。
 */
export {};
