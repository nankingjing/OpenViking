## OpenViking memory extraction 拒答 case 分析（2026-06-13）

### Trace 79773750c7d0d65cde0ab959c480fe30

**现象**

`ExtractLoop` 在 memory extraction 阶段连续 4 次收到非 JSON 拒答文本，导致 `_call_llm` 解析失败：

```text
iteration 1/3: 抱歉，您的问题我无法识别。
iteration 2/4: 您的问题我无法回答。
iteration 3/4: 你好，我无法给到相关内容。
iteration 4/4: 抱歉，我无法回答这个问题。
```

trace 中对应 `volcengine.vlm.call` 的 `response.usage` 为：

```text
completion_tokens=0 prompt_tokens=0 total_tokens=0
```

这说明请求大概率没有正常进入模型计费/生成链路，而是在上游网关或模型前置层被统一兜底拒答。

**排查结论**

1. 不是 `delete_ids` / JSON schema 变更导致。因为在 memory extraction 之前，Working Memory 压缩调用也已经拒答：

```text
你好，这个问题我无法回答，很遗憾不能帮助你。
```

2. 不是 quote/friend 那段触发。下面这句单独测试正常：

```text
This was written to me by a friend who, unfortunately, will never be able to support me. I miss him here. This quote says "Let go of what no longer serves you."
```

3. 完整 conversation 本身会触发拒答，即使不带完整 memory schema：

```text
conversation_only_json => 您的问题我无法回答。
wm_full_prompt_from_trace => 你好，我无法给到相关内容。
extract_no_schema => 抱歉，这个问题未找到相关结果。
extract_full_schema_control_conversation => 正常 JSON
extract_full_schema_trace_conversation => 抱歉，我无法回答这个问题。
```

4. 进一步二分定位，实际触发文本是第 3 条用户消息，单独输入也会被拒答：

```text
Jolene: This country was awesome! It showed me different kinds of yoga and their backgrounds, which made me appreciate it even more. We visited a lot of delicious cafes! Have you ever been somewhere that was important to you?
```

该句中文翻译后不会被拒答：

```text
这个国家太棒了！它让我了解了不同类型的瑜伽及其背景，这让我更加欣赏瑜伽了。我们还去了很多好吃的咖啡馆！你有没有去过对你来说很重要的地方？
```

中文测试返回正常 JSON：

```json
{
  "type": "良性对话",
  "positive_feedback": "这个国家太棒了，通过它了解了不同类型的瑜伽及其背景，更加欣赏瑜伽，还去了很多好吃的咖啡馆",
  "question": "你有没有去过对你来说很重要的地方？"
}
```

**疑似触发点**

英文原句中的组合：

```text
This country ... different kinds of yoga and their backgrounds
```

可能被上游风控误判为和 country / kinds / backgrounds 相关的敏感群体或身份背景抽取。语义本身是良性的，中文同义表达不触发。

**建议**

- 在 `ExtractLoop` 中检测 canned refusal：例如包含“无法回答 / 无法识别 / 无法给到 / 未找到相关结果 / 抱歉”等，且响应不是 JSON。
- 对这种拒答不要继续 retry 4 次，可直接降级为空操作：

```json
{
  "delete_ids": [],
  "links": [],
  "events": [],
  "tools": [],
  "soul": [],
  "cases": [],
  "entities": [],
  "identity": [],
  "preferences": [],
  "skills": [],
  "profile": []
}
```

- 如果 VLM SDK 能拿到 `finish_reason` / safety reason / content_filter 信息，应写入 trace，避免只能通过 canned refusal 文案和 usage=0 推断。

