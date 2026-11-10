/** Schedule the next poll after settlement, and abort active work when its view is disposed. */
export function serialPoll(
  task: (signal: AbortSignal) => Promise<void>,
  delay: number,
  onError: (error: unknown) => void,
): () => void {
  const controller = new AbortController();
  let stopped = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const run = async () => {
    try {
      await task(controller.signal);
    } catch (error) {
      if (!stopped) onError(error);
    } finally {
      if (!stopped) timer = setTimeout(() => void run(), delay);
    }
  };
  void run();
  return () => {
    stopped = true;
    controller.abort();
    clearTimeout(timer);
  };
}
