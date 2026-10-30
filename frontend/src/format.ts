/** Money is always integer KES, displayed like "KES 23,500". */
export function formatKES(amountKes: number): string {
  return `KES ${Math.round(amountKes).toLocaleString('en-KE')}`
}
