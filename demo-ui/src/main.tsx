import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

// 폰트는 번들에 넣는다. CDN 이 막힌 환경에서 조용히 폴백되면 대비·줄바꿈 검증이 흔들린다.
import "@fontsource/pretendard/400.css";
import "@fontsource/pretendard/600.css";

import "./styles/tokens.css";
import "./styles/reset.css";
import "./styles/global.css";

import App from "./App";

const container = document.getElementById("root");
if (!container) throw new Error("#root 를 찾지 못했습니다");

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
