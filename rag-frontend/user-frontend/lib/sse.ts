/** SSE 清洗器：删除不面向终端用户的原始诊断字段，再流式返回安全事件。 */
function sanitizeEventBlock(block: string): string {
  const lines = block.split(/\r?\n/u);
  const dataLines = lines.filter((line) => line.startsWith("data:"));
  if (!dataLines.length) return block;
  const raw = dataLines.map((line) => line.slice(5).trimStart()).join("\n");
  try {
    const data = JSON.parse(raw) as Record<string, unknown>;
    if (Array.isArray(data.warnings)) {
      data.warnings = data.warnings.filter((warning) => !String(warning).toLowerCase().includes("rerank"));
    }
    return [...lines.filter((line) => !line.startsWith("data:")), `data: ${JSON.stringify(data)}`].join("\n");
  } catch {
    return block;
  }
}

export function sanitizedSseBody(source: ReadableStream<Uint8Array>): ReadableStream<Uint8Array> {
  // 按双换行维护 SSE 帧边界；跨网络分块的不完整帧留到下一个 chunk 再处理。
  const reader = source.getReader();
  const decoder = new TextDecoder();
  const encoder = new TextEncoder();
  let buffer = "";
  return new ReadableStream<Uint8Array>({
    async pull(controller) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done });
      const blocks = buffer.split(/\r?\n\r?\n/u);
      buffer = blocks.pop() ?? "";
      for (const block of blocks) controller.enqueue(encoder.encode(`${sanitizeEventBlock(block)}\n\n`));
      if (done) {
        if (buffer.trim()) controller.enqueue(encoder.encode(`${sanitizeEventBlock(buffer)}\n\n`));
        controller.close();
      }
    },
    cancel() {
      return reader.cancel();
    },
  });
}
