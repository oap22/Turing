/** One outstanding write per terminal. Input arriving behind a slow write is
 * combined into the next IPC call without adding a timer to isolated keys. */
export function createPtyInput(write: (data: string) => Promise<unknown>, onError: (error: unknown) => void) {
  let pending: string[] = [];
  let writing = false;
  let closed = false;
  async function drain() {
    if (writing || closed) return;
    writing = true;
    try {
      while (!closed && pending.length) {
        const data = pending.join("");
        pending = [];
        await write(data);
      }
    } catch (error) {
      // A partial write cannot safely be retried: that would duplicate input.
      closed = true;
      pending = [];
      onError(error);
    } finally {
      writing = false;
    }
  }
  return {
    push(data: string) { if (!closed) { pending.push(data); void drain(); } },
    close() { closed = true; pending = []; },
  };
}
