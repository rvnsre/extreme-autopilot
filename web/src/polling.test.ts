import { afterEach, describe, expect, it, vi } from "vitest";
import { serialPoll } from "./polling";

afterEach(() => vi.useRealTimers());
describe("serial polling", () => {
  it("never overlaps slow requests and stops future work on disposal", async () => {
    vi.useFakeTimers();
    let release!: () => void;
    const task = vi.fn(
      (_signal: AbortSignal) =>
        new Promise<void>((resolve) => {
          release = resolve;
        }),
    );
    const stop = serialPoll(task, 2500, vi.fn());
    await vi.advanceTimersByTimeAsync(10000);
    expect(task).toHaveBeenCalledTimes(1);
    release();
    await vi.advanceTimersByTimeAsync(2500);
    expect(task).toHaveBeenCalledTimes(2);
    const signal = task.mock.calls[1][0] as AbortSignal;
    stop();
    expect(signal.aborted).toBe(true);
    release();
    await vi.advanceTimersByTimeAsync(10000);
    expect(task).toHaveBeenCalledTimes(2);
  });
  it("ignores failures from an aborted, obsolete view", async () => {
    let reject!: (error: Error) => void;
    const failed = vi.fn();
    const stop = serialPoll(
      () =>
        new Promise<void>((_, no) => {
          reject = no;
        }),
      10,
      failed,
    );
    stop();
    reject(new Error("old response"));
    await Promise.resolve();
    expect(failed).not.toHaveBeenCalled();
  });
});
