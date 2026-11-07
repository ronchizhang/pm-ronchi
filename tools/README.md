# tools

这两个脚本是给「维护者」用的，使用者装完技能不需要跑它们。

| 文件 | 干什么 | 什么时候跑 |
|:--|:--|:--|
| `preflight.py` | 推送前体检五项：结构 / 本机痕迹 / 可运行 / 一致性 / 卫生 | 每次改完内容、推之前 |
| `push_gate.py` | 推送闸门：没有明确确认，一行都不推 | 每次推之前 |

```bash
python3 tools/preflight.py               # 五项全绿才进入下一步
python3 tools/push_gate.py status        # 看一眼这一屏
python3 tools/push_gate.py approve "可以发"   # 用户点头之后，把他的原话记下来
python3 tools/push_gate.py push          # 校验通过才真推
```

批准绑定 HEAD、远端地址、工作区是否干净三样东西。任何一样变了，批准立刻作废——
你确认过的那一版已经不在了。

`preflight.py` 只判「这一版能不能推出去」，不判内容写得好不好。内容好不好，人看。
