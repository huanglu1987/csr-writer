"""Content-bound, batched reviewer decisions for project drafts."""
from datetime import date
import hashlib
import json

from .errors import ConfigurationError
from .io_utils import atomic_write_json, load_json_unique


def digest(value):
    raw=json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",",":"))
    return hashlib.sha256(raw.encode()).hexdigest()


class Decisions:
    def __init__(self, path):
        batch=load_json_unique(path) if path else {}
        self.mode=batch.get("mode","project")
        if self.mode not in {"project","simulation"}:
            raise ConfigurationError("答复模式无效")
        self.study=batch.get("study")
        self.input={}
        self.items=[]
        if batch and (not isinstance(batch.get("answers"),list) or not self.study):
            raise ConfigurationError("答复须列明研究编号与答复条目")
        seen=set()
        for answer in batch.get("answers",[]):
            key=answer.get("id")
            if not key or key in seen:
                raise ConfigurationError("答复编号缺失或重复")
            seen.add(key)
            if any(answer.get(k) for k in ("choice","reviewer","date","statement")):
                self.input[key]=answer

    def bind(self, study, simulated):
        if self.study and study!=self.study:
            raise ConfigurationError("答复研究与方案不一致")
        if self.input and simulated!=(self.mode=="simulation"):
            raise ConfigurationError("演练答复与真实研究不得混用")
        self.study=study
        self.mode="simulation" if simulated else "project"

    def ask(self, key, role, prompt, sections, evidence, choices, *, dependencies=None):
        basis=digest([self.study,key,role,choices,evidence if dependencies is None else dependencies])
        item=dict(id=key,role=role,action=prompt,sections=sections,basis=basis,
                  evidence=evidence,choices=choices,status="open")
        self.items.append(item)
        answer=self.input.get(key)
        if not answer:
            return None
        if answer.get("basis")!=basis:
            item["status"]="stale"
            return None
        if (answer.get("role")!=role or answer.get("choice") not in choices
                or not str(answer.get("reviewer","")).strip() or not str(answer.get("statement","")).strip()):
            raise ConfigurationError("答复缺少适用的岗位、选项、答复人或依据："+key)
        try:
            date.fromisoformat(answer["date"])
        except (KeyError,TypeError,ValueError):
            raise ConfigurationError("答复日期无效："+key)
        item.update(status="deferred" if answer["choice"]=="待评估" else "answered",answer={k:answer[k] for k in ("choice","reviewer","date","statement")})
        return item["answer"]

    def save(self,out):
        unused=sorted(set(self.input)-{i["id"] for i in self.items})
        atomic_write_json(out/"负责人决定.json",dict(study=self.study,mode=self.mode,items=self.items,unused_answers=unused))
        template=[{**{k:i[k] for k in ("id","role","basis","action","choices","sections")},
                   "reviewer":"","date":"","choice":"","statement":""} for i in self.items if i["status"]!="answered"]
        for row in template:
            if row["id"]=="MED-CONCLUSION":
                row["applies_if"]={k:"" for k,_,_ in next(i["evidence"]["dependent_choices"] for i in self.items if i["id"]==row["id"])}
        atomic_write_json(out/"集中答复模板.json",dict(study=self.study,mode=self.mode,answers=template))
