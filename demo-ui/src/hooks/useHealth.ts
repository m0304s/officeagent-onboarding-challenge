// 의존성 상태 폴링. 조회가 실패하면 즉시 `unknown` 으로 덮는다 — 마지막 성공값을
// 유지하면 서버가 죽은 화면이 계속 정상으로 보인다.

import { useEffect, useState } from "react";

import { fetchHealth } from "../api/client";
import type { HealthReport } from "../api/types";

const POLL_INTERVAL_MS = 10_000;

export type HealthState = { status: "unknown" } | { status: "known"; report: HealthReport };

export function useHealth() {
  const [state, setState] = useState<HealthState>({ status: "unknown" });

  useEffect(() => {
    const controller = new AbortController();
    let timer: number | undefined;

    const poll = async () => {
      // 숨은 탭에서 10초마다 깨우지 않는다. 사용자가 안 보는 상태를 갱신할 이유가 없다.
      if (document.visibilityState === "hidden") return;
      try {
        setState({ status: "known", report: await fetchHealth(controller.signal) });
      } catch {
        setState({ status: "unknown" });
      }
    };

    void poll();
    timer = window.setInterval(() => void poll(), POLL_INTERVAL_MS);
    document.addEventListener("visibilitychange", poll);

    return () => {
      controller.abort();
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", poll);
    };
  }, []);

  return state;
}
