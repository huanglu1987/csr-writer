"""Read supported prespecified statistical methods from the effective SAP, with paragraph evidence."""
import re
from collections import defaultdict
import unicodedata
from .project_sources import eligible_record


HEADINGS = {
    "cox": ("时间至事件分析","生存分析"),
    "categorical": ("分类终点参考组插补","分类疗效纵向分析"),
    "cmh": ("分层二分类分析","分层应答分析"),
    "descriptors": ("补充描述统计", "其他描述性统计","实验室与生命体征"),
    "groups": ("分析人群与分组", "分析集与分组", "分析人群", "分析集定义","分析人群及一般统计约定"),
    "efficacy": ("主要疗效分析", "主要终点分析", "主要评价指标分析","主要评价变量与统计推断"),
    "safety": ("安全性分析", "不良事件分析","暴露及不良事件评价"),
}
# Match the complete clause: an exception or qualification cannot silently disappear.
RULES = {
    "groups": {
        "arms": r"(?:治疗|报告)组别[：:]\s*(?P<arms>[^。；;]+)",
        "disposition": r"按随机组列出完成研究与提前结束的人数",
        "demographics": r"按随机组报告年龄均值和样本标准差，以及女性人数和百分比",
        "itt": r"(?:ITT|意向治疗集)[：:]?\s*(?:纳入|包括)(?:全部|所有)随机受试者，按随机(?:分配)?组(?:别)?分析",
        "saf": r"(?:SAF|安全性分析集)[：:]?\s*(?:纳入|包括)至少实际用药一次的受试者，按实际(?:用药|治疗)组(?:别)?分析",
    },
    "efficacy": {
        "response": r"主要终点为第\s*(?P<week>\d+)\s*周(?P<endpoint>[^（(，。]+)[（(](?P<code>[A-Za-z0-9_-]+)[）)]较基线(?P<direction>下降|上升)至少\s*(?P<threshold>\d+(?:\.\d+)?)\s*分的应答率",
        "change": r"主要终点为第\s*(?P<week>\d+)\s*周(?P<endpoint>[^（(，。]+)[（(](?P<code>[A-Za-z0-9_-]+)[）)]相对基线的变化量",
        "observed": r"按随机组对基线和窗内终点均有测量的观察病例进行描述，缺失不插补，救援后测量保留",
        "descriptive": r"变化量按随访减基线计算，报告均值、样本标准差和观察人数，不作组间推断",
        "range": r"量表范围为\s*(?P<low>-?\d+(?:\.\d+)?)\s*(?:至|到|~|～)\s*(?P<high>-?\d+(?:\.\d+)?)\s*分",
        "baseline": r"基线取随机前\s*(?P<baseline_days>\d+)\s*日至随机当日(?:之间)?标记为\s*BL\s*的唯一测量",
        "window": r"随机当日为第\s*1\s*天，目标日为第\s*(?P<target>\d+)\s*天，访视窗为第\s*(?P<start>\d+)\s*至\s*(?P<end>\d+)\s*天，取距目标日最近的测量，等距时取(?P<tie>较早|较晚)者",
        "missing": r"ITT\s*中基线或窗内终点评分缺失者按(?:未|无)应答处理",
        "rescue": r"随机至第\s*(?P<rescue_day>\d+)\s*天[（(]含当日[）)]使用救援治疗者按(?:未|无)应答处理",
        "interval": r"各组应答率采用双侧\s*(?P<confidence>\d+(?:\.\d+)?)\s*%\s*Wilson\s*置信区间",
        "contrast": r"组间报告未调整率差[（(](?P<test>[^（）()，]+)减(?P<ref>[^（）()，]+)[）)]，采用双侧\s*(?P<rd_confidence>\d+(?:\.\d+)?)\s*%\s*Newcombe\s*无连续性校正置信区间，不作显著性检验",
    },
    "safety": {
        "window": r"TEAE\s*观察窗为首次实际用药日至末次实际用药后\s*(?P<follow>\d+)\s*日，含首尾两日",
        "emergence": r"窗内新发事件及首次用药前已发生而在窗内加重的事件计为\s*TEAE",
        "counts": r"按实际治疗组报告发生\s*TEAE\s*的人数、百分比和事件数，同一人同一首选术语只计一人",
        "categories": r"按研究者记录汇总严重、致死、药物相关和\s*(?P<grade>[1-5])\s*级及以上\s*TEAE，相关性待定者单列",
        "related_terms": r"相关性为(?P<related_terms>[^，]+)计入药物相关，无关不计入，待评估单列",
        "exposure": r"暴露天数按每人实际正剂量给药的不同日期计数，报告组内总人日",
    },
}


RULES["cox"]={"cox_rule":r"ITT按随机组分析(?P<outcome>[^（(，]+)[（(](?P<code>[A-Za-z0-9_]+)[）)]，随机日为第1天，至首次事件，未发生者在末次确认日或第(?P<cut>[0-9]+)天的较早日删失，拟合仅含组别的Cox模型，同日事件用Breslow法，报告(?P<test>[^，]+?)相对(?P<ref>[^，]+?)的风险比、双侧(?P<level>[0-9.]+)%Wald置信区间及P值"}
RULES["cmh"]={"cmh_rule":r"ITT按随机组分析(?P<outcome>[^（(，]+)[（(](?P<code>[A-Za-z0-9_]+)[）)]，采用第(?P<target>[0-9]+)天前后(?P<margin>[0-9]+)天内唯一任务评估，未测者按未完成处理，按随机登记层作双侧CMH检验，无连续性校正，报告(?P<test>[^，]+?)相对(?P<ref>[^，]+?)的共同优势比及双侧(?P<level>[0-9.]+)%RBG置信区间"}
RULES["categorical"] = {
    "outcome": r"次要终点为(?P<outcome>[^（(，]+)[（(](?P<code>[A-Za-z0-9_]+)[）)]，类别依次为(?P<categories>[^，]+)，应答类别为(?P<response_levels>[^，]+)，首类为对数优势参照",
    "visits": r"随机日为第1天，目标访视日为(?P<cat_days>[0-9、]+)，各窗前后(?P<margin>[0-9]+)天，取距目标最近且等距时较早的分类实测",
    "model": r"按随机组分别估计全访视类别轨迹，采用每条轨迹参数为(?P<prior>[0-9.]+)的对称Dirichlet先验，试验组以永久停药当日及以前的名义访视拟合，参考组使用全部访视，按所有实测类别条件抽样",
    "strategy": r"预设策略为(?P<strategies>(?:MAR|J2R|CIR)(?:、(?:MAR|J2R|CIR))*)，以(?P<ref>[^，]+)为参考，J2R取参考组边际分布，CIR保留停药前末个计划访视的类别对数优势差，首访前差为零，按参考组停药后条件分布及指定边际作比例拟合",
    "inference": r"(?P<m>[0-9]+)条独立数据增广链各(?P<sweeps>[0-9]+)轮，随机种子(?P<seed>[0-9]+)，ITT中末访按中心(?P<site>[A-Za-z][A-Za-z0-9_]*)随机人数占比标准化，报告(?P<test>[^，]+?)减参考组的应答率差及双侧(?P<level>[0-9.]+)%区间和P值，按中心内两组样本方差估计方差，以随机人数减两倍中心数为完整自由度，采用Rubin合并与Barnard-Rubin自由度",
}
RULES["descriptors"] = {
    "lab": r"安全集按实际组汇总(?P<lab_code>[A-Za-z0-9_]+)在BL和(?P<lab_visit>[A-Za-z0-9_]+)的观察人数、均值及配对变化，临床重要异常按研究者标记逐人计数",
    "vital": r"安全集按实际组汇总(?P<vs_code>[A-Za-z0-9_]+)在BL和(?P<vs_visit>[A-Za-z0-9_]+)的观察人数、均值及配对变化",
}
MODEL_RULES = {
    "visits": r"模型访视目标日(?:为|包括)(?P<days>[0-9、，及和与 ]+)，各访视窗为目标日前后(?P<margin>[0-9]+)天",
    "policy": r"以随访减基线为变化量，按随机组分析，保留停药及救援后测量，基线必须完整",
    "comparison": r"比较(?P<test>[^，。]+?)减(?P<ref>[^，。]+?)的终点变化量，报告双侧(?P<model_conf>[0-9.]+)%置信区间及P值",
    "covariates": r"额外协变量适用于(?P<adjust_models>(?:MMRM|ANCOVA|FCS)(?:、(?:MMRM|ANCOVA|FCS))*)，分类变量为(?P<cat_cols>[^，]+)，连续变量为(?P<num_cols>[^，]+)，采用共同斜率及固定效应",
    "missing_table": r"按随机组报告各访视实测与缺失人数、间歇缺失人数及永久停药后的实测与缺失次数",
    "fcs": r"(?P<fcs_role>主要|支持性|敏感性)分析采用MAR下正态回归FCS，按访视顺序用治疗组、基线及其余各访视变化量循环插补，以各访视观察均值初始化，(?P<fcs_m>[0-9]+)条独立链各迭代(?P<sweeps>[0-9]+)轮，随机种子(?P<fcs_seed>[0-9]+)，各取末轮作终点ANCOVA，按Rubin规则及(?P<fcs_df>Barnard-Rubin|Rubin)自由度合并",
    "center": r"(?:中心随机截距|中心标识)取自基线列(?P<center_col>[A-Za-z][A-Za-z0-9_]*)，中心间相互独立，采用REML及期望信息Satterthwaite自由度",
    "mixed": r"(?P<mixed_role>主要|支持性|敏感性)分析采用MMRM，固定效应为分类访视、组别与访视交互及基线与访视交互，(?P<center_form>中心随机截距与治疗随机斜率相关|仅含中心随机截距|无中心随机效应)，受试者内采用(?P<residual_form>按治疗组分别估计的|共同的)非结构化残差协方差，基于MAR使用可用访视，采用REML及期望信息Satterthwaite推断",
    "rmmrm": r"(?P<rmmrm_role>主要|支持性|敏感性)分析采用随机中心截距MMRM，固定效应为分类访视、组别与访视交互及基线与访视交互，受试者内采用共同非结构化残差协方差，基于MAR使用可用访视",
    "rancova": r"(?P<rancova_role>主要|支持性|敏感性)分析采用随机中心截距ANCOVA，对终点观察病例以治疗组和基线为固定效应，个体残差独立同方差",
    "nar": r"(?P<nar_role>敏感性)分析采用轮内偏移FCS，在每轮每次条件抽样后仅对永久停药次日起按名义访视日判定的缺失值加偏移，并将更新值用于下一条件模型，按(?P<nar_first>[^，]+?)和(?P<nar_second>[^，]+?)顺序的情景分别为(?P<nar_deltas>[-+0-9./、 ]+)分，其余参数和分析同MAR FCS",
    "rb_covariates": r"参考组插补额外调整分类变量(?P<rb_cat_cols>[^，]+)，连续变量(?P<rb_num_cols>[^，]+)，在插补模型中各访视分别估计系数，在终点ANCOVA中调整相同变量",
    "ref_definition": r"研究药组在停药后缺失时，J2R采用对照组相应访视的均值轨迹，CIR保留停药时已获得的平均组间差并沿对照轨迹变化",
    "rbmi": r"(?P<rbmi_role>敏感性)分析采用参考组插补(?P<strategies>J2R、CIR|CIR、J2R|J2R|CIR)，参考组为(?P<ref_arm>[^，]+)，以名义访视日判定永久停药后访视，试验组停药前及参考组全部实测拟合(?P<ref_design>治疗组和基线的访视别均值与共同非结构化协方差|各治疗组分别含截距及基线的访视别回归和组别特异非结构化协方差，停药前采用本组联合分布，停药后采用参考组的条件回归系数和条件协方差)，基于全部实测条件插补，以基线变化零为首访前锚点，采用贝叶斯正态数据增广，(?P<ref_m>[0-9]+)条独立链各迭代(?P<ref_sweeps>[0-9]+)轮，随机种子(?P<ref_seed>[0-9]+)，各取末轮作终点ANCOVA，按Rubin规则及(?P<ref_df>Barnard-Rubin|Rubin)自由度合并",
    "mnar": r"(?P<mnar_role>敏感性)分析采用MNAR模式混合Delta调整，在各MAR FCS完成数据中仅对永久停药次日起按名义访视日判定的缺失值加偏移，按(?P<delta_first>[^，]+?)和(?P<delta_second>[^，]+?)顺序的情景分别为(?P<deltas>[-+0-9./、 ]+)分，保留实测值并按相同终点ANCOVA及合并规则分析",
    "mmrm": r"(?P<mmrm_role>主要|支持性|敏感性)分析采用(?:MMRM|重复测量混合模型)，(?:固定效应为|纳入)治疗组、分类访视、组别与访视交互及基线(?P<base_inter>与访视交互)?，(?:采用)?REML估计共同非结构化协方差，按(?P<inference>渐近正态|期望信息Satterthwaite|Kenward-Roger(?:[(]直接协方差参数，期望信息[)])?)推断，基于MAR使用可用访视",
    "ancova": r"(?P<ancova_role>主要|支持性|敏感性)分析采用(?:ANCOVA|协方差分析)，终点观察病例按治疗组和基线拟合普通最小二乘模型，采用残差自由度的t分布",
    "mi": r"(?P<mi_role>主要|支持性|敏感性)分析采用(?:多重插补|MI)，在MAR下依访视顺序以治疗组、基线及此前各访视变化量作单调正态回归插补，插补(?P<m>[0-9]+)次，随机种子(?P<seed>[0-9]+)，每次按治疗组和基线拟合终点ANCOVA，按Rubin规则合并，采用(?P<mi_df>Barnard-Rubin|Rubin)自由度",
}
RULES["efficacy"].update(MODEL_RULES)


def compact(text):
    return text.strip().rstrip("。；;").strip()


def canonical_sentence(text):
    """Only normalize spelling and established equivalent phrases; retain the original citation."""
    text=unicodedata.normalize("NFKC",text).replace(",", "，")
    for old,new in (("意向性治疗集(ITT)","ITT"),("意向治疗集(ITT)","ITT"),
                    ("安全性分析集(SAF)","SAF"),("所有已随机化的受试者","全部随机受试者"),
                    ("至少接受过一次研究用药的受试者","至少实际用药一次的受试者"),
                    ("按照随机分配的治疗组进行分析","按随机分配组分析"),
                    ("按照实际接受的治疗组进行分析","按实际治疗组分析"),
                    ("主要疗效终点为","主要终点为"),("相对于基线","相对基线"),
                    ("改善幅度不低于","改善至少"),("降低不少于","下降至少"),
                    ("降低至少","下降至少"),("升高至少","上升至少"),
                    ("较基线降低≥","较基线下降至少"),("较基线升高≥","较基线上升至少"),
                    ("分值范围为","量表范围为"),("缺失数据不作插补","缺失不插补"),
                    ("不进行组间推断","不作组间推断"),("不进行显著性检验","不作显著性检验"),
                    ("置信限","置信区间"),("不加连续性校正","无连续性校正")):
        text=text.replace(old,new)
    return text.replace("Kenward–Roger","Kenward-Roger").replace("δ","Delta")


def additional_scopes(records):
    scopes=[]
    for item in records:
        if not eligible_record(item):
            continue
        label=item["text"].strip().strip("# ").rstrip("：:")
        if len(label)<38 and re.fullmatch(r"(?:[0-9.]+\s*)?(?:次要(?:疗效|终点)|敏感性|亚组|药代动力学|实验室(?:参数)?)(?:的)?(?:统计)?分析",label):
            scopes.append(f"{item['location']}「{label}」需按所述方法另行完成并核对相应输出。")
    return scopes


def parse_sap(records, reading=(), unresolved=None):
    from .errors import ConfigurationError
    replacements={}
    for item in reading:
        anchor=(item["section"],item["location"],item["quote"])
        if anchor in replacements:
            raise ConfigurationError("SAP 智能体解读坐标重复")
        replacements[anchor]=item
    from .sap_language import FORMS,compile_prose
    sections = defaultdict(list)
    active = None
    outside=[]
    for rec in records:
        if not eligible_record(rec):
            continue
        for line in rec["text"].splitlines():
            title = re.sub(r"^(?:#+\s*|\d+(?:\.\d+)*[.、]?\s+)", "", line.strip()).rstrip("：:")
            heading = next((k for k, options in HEADINGS.items() if title in options), None)
            if heading or line.strip().startswith("#") or str(rec.get("paragraph_style") or "").lower().startswith(("title","heading")):
                active = heading
            elif active:
                for clause in re.split(r"[。；;]",line):
                    if compact(clause):
                        sections[active].append((compact(clause),rec["location"]))
            else:
                for sentence in (compact(s) for s in re.split(r"[。；;]",line) if compact(s)):
                    plain=canonical_sentence(sentence)
                    owners=[name for name,rules in RULES.items() if any(re.fullmatch(rx,plain,re.I) for rx in rules.values())]
                    if not owners:
                        spans=[canonical_sentence(x.strip()) for x in re.split("[，,]",sentence)]
                        for owner in RULES:
                            if all(any(re.fullmatch(rx,fragment,re.I) for (family,_),(_,patterns) in FORMS.items() if family==owner for rx in patterns) for fragment in spans):
                                owners.append(owner)
                    if len(owners)==1:
                        sections[owners[0]].append((sentence,rec["location"]))
                    else:
                        outside.append((sentence,rec["location"]))
    values, evidence, issues = {}, {}, {}
    for name,rules in RULES.items():
        found = {}
        errors = []
        clauses,prose=compile_prose(name,sections[name],canonical_sentence,rules)
        for sentence,loc in clauses:
            match = next(((key,m) for key,rx in rules.items() if (m:=re.fullmatch(rx,canonical_sentence(sentence),re.I))),None)
            original=sentence
            item=replacements.pop((name,loc,sentence),None)
            if item is not None:
                if match is not None:
                    raise ConfigurationError("智能体答复覆盖了已明确解析的 SAP 规则")
                normalized=item["normalized"]
                rx=rules.get(item["rule"])
                m=re.fullmatch(rx,canonical_sentence(normalized),re.I) if rx else None
                # Preserve all explicit numbers and identifiers, and reject conditional exceptions.
                tokens=lambda x:sorted(re.findall(r"[A-Za-z][A-Za-z0-9_-]*|[-+]?\d+(?:\.\d+)?",canonical_sentence(x).casefold()))
                if (not m or tokens(sentence)!=tokens(normalized) or
                    re.search(r"除外|除非|但|仅当|若|例外|而非|禁止|不再|死亡|竞争事件",sentence)):
                    raise ConfigurationError("SAP 解读改变数值、标识、例外或未匹配受支持的方法："+loc)
                if not str(item.get("rationale","")).strip():
                    raise ConfigurationError("SAP 解读缺少逐条语义依据")
                sentence=normalized
                match=(item["rule"],m)
            if match is None:
                if unresolved is not None:
                    unresolved.append(dict(section=name,location=loc,quote=original,available_rules=list(rules)))
                errors.append(f"{loc} 的条款未能完整执行「{sentence}」")
                continue
            key,m = match
            if key in found and found[key]["parameters"] != m.groupdict():
                errors.append(f"{loc} 与同节的 {key} 规则不同")
            found[key] = {"location":loc, "quote":original, "parameters":m.groupdict()}
            if item is not None:
                found[key].update(normalized=sentence,reading="宿主智能体逐句解读",rationale=item["rationale"])
        required=set(rules)-{"disposition","demographics"}
        if name=="descriptors" or (name in {"cox","cmh","categorical"} and not sections[name]):
            required=set()
        if name=="efficacy":
            binary={"response","missing","rescue","interval","contrast"}
            continuous={"change","observed","descriptive"}
            models = {"mmrm","ancova","mi","fcs","mnar","nar","rbmi","rmmrm","rancova","mixed"}.intersection(found)
            if models:
                required = {"change","range","baseline","window","visits","policy","comparison"} | models
                if (binary | {"observed","descriptive"}).intersection(found):
                    errors.append("模型分析与描述性或二分类规则冲突")
                roles = [found[k]["parameters"][k+"_role"] for k in models]
                if {"mnar","nar"}.intersection(models) and "fcs" not in found:
                    errors.append("MNAR偏移要求SAP预设相应的MAR FCS")
                has_center=bool({"rmmrm","rancova"}.intersection(models)) or ("mixed" in models and found["mixed"]["parameters"]["center_form"]!="无中心随机效应")
                if has_center != ("center" in found):
                    errors.append("随机中心模型与中心来源列必须同时明确")
                if "rb_covariates" in found and "rbmi" not in models:
                    errors.append("参考组插补协变量缺少对应模型")
                if "ref_definition" in found and ("rbmi" not in models or set(found["rbmi"]["parameters"]["strategies"].upper().split("、"))!={"J2R","CIR"}):
                    errors.append("参考组假设说明和拟执行情景不一致")
                if "rbmi" in models and found["rbmi"]["parameters"]["ref_arm"] != found.get("comparison",{}).get("parameters",{}).get("ref"):
                    errors.append("参考组插补指定组与对比参考组不一致")
                if "covariates" in found:
                    targets=found["covariates"]["parameters"]["adjust_models"].lower().split("、")
                    if not set(targets)<=models or len(targets)!=len(set(targets)):
                        errors.append("额外协变量的模型归属不唯一或相应模型未定义")
                if roles.count("主要") != 1:
                    errors.append("主要分析未能唯一核对")
            elif set(MODEL_RULES).intersection(found):
                errors.append("缺少可执行的模型条款")
            else:
                required -= set(MODEL_RULES)
            if not models and "change" in found and not binary.intersection(found):
                required-=binary
            elif "response" in found and not continuous.intersection(found):
                required-=continuous
            for sentence,loc in outside:
                if re.search(r"死亡|救援|停药|中间事件",sentence) and re.search(r"视为|按未应答|排除|剔除|不计入|不纳入|截尾|赋值|复合策略|假想策略|治疗策略",sentence):
                    errors.append(f"{loc} 的中间事件处理策略未绑定至具体终点「{sentence}」")
                if re.search(r"主要(?:终点|分析|疗效)|ITT|救援|缺失",sentence,re.I) and re.search(r"MMRM|ANCOVA|Cox|FCS|MNAR|J2R|CIR|参考组插补|随机中心|Kenward|多重插补|logistic|分层|协方差分析|重复测量模型|混合效应模型|末次观察值|不纳入|剔除",sentence,re.I):
                    errors.append(f"{loc} 出现尚未执行的模型条款「{sentence}」")
        if name=="safety":
            for sentence,loc in outside:
                if re.search(r"TEAE|安全性分析|安全集",sentence,re.I) and re.search(r"观察窗|末次|首次|实际|随机|仅|排除|另行",sentence):
                    errors.append(f"{loc} 存在尚未解决的安全性分析条款「{sentence}」")
        missing = [k for k in rules if k in required and k not in found]
        if missing:
            errors.append("缺少可唯一解释的规则 " + "、".join(missing))
        for key,anchors in prose.items():
            if key in found:
                found[key].update(normalized=found[key]["quote"],quote="。".join(a["quote"] for a in anchors),anchors=anchors,reading="自然语言分句提取")
        evidence[name]=found
        values[name]={k:(v or "").strip() for item in found.values() for k,v in item["parameters"].items()}
        if name=="efficacy":
            if "inference" in values[name]:
                inf=values[name]["inference"]
                if inf.lower().startswith("kenward-roger"):
                    values[name]["kr_convention_explicit"]="(" in inf
                    values[name]["inference"]="Kenward-Roger（直接协方差参数，期望信息）"
                elif inf.lower()=="期望信息satterthwaite":
                    values[name]["inference"]="期望信息Satterthwaite"
            for key in ("mi_df","fcs_df","ref_df"):
                if key in values[name]:
                    values[name][key]="Rubin" if values[name][key].lower()=="rubin" else "Barnard-Rubin"
            if "missing_table" in found:
                values[name]["missing_table"]=True
            values[name]["method"]="change" if "change" in found else "response"
        if errors:
            issues[name]="；".join(errors)
    if replacements:
        raise ConfigurationError("SAP 解读所引段落或完整原句与当前有效原件不符")
    return values,evidence,issues
