export const terminal = new Set([
  "completed",
  "partial",
  "blocked",
  "failed",
  "cancelled",
]);
export const labels: Record<string, string> = {
  queued: "Növbədə",
  running: "İcra olunur",
  recovering: "Bərpa edilir",
  completed: "Tamamlandı",
  partial: "Qismən tamamlandı",
  blocked: "Maneə var",
  failed: "Xəta",
  cancelled: "Dayandırıldı",
};
export function briefLength(text: string): number {
  return Array.from(text.trim()).length;
}
export function money(value: number): string {
  return "$" + value.toFixed(3);
}
