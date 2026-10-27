import { createRoot } from "react-dom/client";
import App from "./App";
import "./style.css";

// 刻意不包 StrictMode：开发态双挂载会重复建立 EventSource，
// 同一轮运行的 node_input/tool_call 会被订阅两遍，事件流凭空翻倍。
const root = document.getElementById("root");
if (!root) throw new Error("#root 缺失");

createRoot(root).render(<App />);
