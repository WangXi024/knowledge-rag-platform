/** PDF 映射纯函数：只校验和缩放接口位置，不读取文件、不访问网络、不猜测缺失坐标。 */
import type { SourcePosition } from './contracts';

/** 返回排序去重的一基页码；零、负数、小数和缺失值不参与跳页。 */
export function sourcePages(positions: SourcePosition[]): number[] {
  return [...new Set(positions.map(({ page }) => page).filter((page): page is number => Number.isInteger(page) && Number(page) > 0))].sort((a, b) => a - b);
}

/**
 * RAGFlow 顺序为 left/right/top/bottom，不是 x1/y1/x2/y2；按页面原尺寸转成百分比。
 * 越界、颠倒、非有限数直接跳过；不能通过裁剪或交换坐标把错误位置伪装成精确映射。
 */
export function sourceRects(positions: SourcePosition[], page: number, width: number, height: number) {
  if (!(Number.isFinite(width) && Number.isFinite(height) && width > 0 && height > 0)) return [];
  return positions.flatMap((position) => {
    const box = position.bbox;
    if (position.page !== page || !box || box.length !== 4 || !box.every(Number.isFinite)) return [];
    const [left, right, top, bottom] = box;
    if (left < 0 || top < 0 || right <= left || bottom <= top || right > width || bottom > height) return [];
    return [{ left: left / width * 100, top: top / height * 100, width: (right - left) / width * 100, height: (bottom - top) / height * 100 }];
  });
}
