"""Compile clinical prose in independently sourced clauses into the existing SAP contract."""
from collections import defaultdict
import re

# The parts of a rule can occur in different sentences or in a different order.
# A partially recognized rule is returned to the strict parser, never completed by default.
FORMS = {
 ("groups","arms"): ("治疗组别：{arms}", [r"(?:随机分组为|研究包含两个平行组|两组分别为)[：:]?\s*(?P<arms>[^，。]+、[^，。]+)"]),
 ("groups","itt"): ("ITT纳入全部随机受试者，按随机组分析", [
     r"(?:所有|全部)(?:已)?随机(?:的)?受试者(?:均)?(?:进入|纳入)ITT",r"(?:ITT|疗效分析)(?:始终)?按(?:最初|原)?随机分配(?:的组别)?(?:归组|分析)"]),
 ("groups","saf"): ("SAF纳入至少实际用药一次的受试者，按实际治疗组分析", [
     r"(?:实际)?至少(?:服用|使用)(?:过)?一次研究药(?:物)?(?:的)?受试者(?:进入|纳入)SAF",r"安全性(?:分析)?按实际(?:服用|使用|接受)的治疗(?:归组|分析)"]),
 ("groups","disposition"): ("按随机组列出完成研究与提前结束的人数",[r"各随机组分别统计完成研究及提前退出的人数"]),
 ("groups","demographics"): ("按随机组报告年龄均值和样本标准差，以及女性人数和百分比",[r"各随机组描述年龄的均值和样本标准差及女性的例数和百分比"]),
 ("efficacy","change"): ("主要终点为第{week}周{endpoint}（{code}）相对基线的变化量",[r"(?:主要疗效指标|主要评价指标)(?:定为|是)第(?P<week>\d+)周(?P<endpoint>[^（(，。]+)[(](?P<code>[A-Za-z0-9_-]+)[)]较基线的(?:改变|变化)"]),
 ("efficacy","range"): ("量表范围为{low}至{high}分",[r"(?:该)?(?:评分|量表)的取值(?:介于|为)(?P<low>-?\d+(?:\.\d+)?)(?:分)?(?:至|到)(?P<high>-?\d+(?:\.\d+)?)分(?:之间)?"]),
 ("efficacy","baseline"): ("基线取随机前{baseline_days}日至随机当日标记为BL的唯一测量",[r"随机前(?P<baseline_days>\d+)天到随机当天(?:期间)?唯一标(?:记|注)为BL的测量作为基线"]),
 ("efficacy","window"): ("随机当日为第1天，目标日为第{target}天，访视窗为第{start}至{end}天，取距目标日最近的测量，等距时取{tie}者",[
     r"将随机当天(?:记作|计作)第1天",r"末访以第(?P<target>\d+)天为目标",r"接受第(?P<start>\d+)至第?(?P<end>\d+)天的测量",
     r"窗内(?:多条记录|多次评估)选最接近目标日的一次",r"(?:距离相同|等距)时取(?P<tie>较早|较晚)(?:的)?(?:一次|记录)"]),
 ("efficacy","visits"): ("模型访视目标日为{days}，各访视窗为目标日前后{margin}天",[r"(?:纳入模型的访视|计划评估)安排在第(?P<days>[0-9、]+)天",r"每次(?:评估|访视)允许前后(?P<margin>\d+)天"]),
 ("efficacy","policy"): ("以随访减基线为变化量，按随机组分析，保留停药及救援后测量，基线必须完整",[
     r"(?:变化量定义为|变化量等于)随访值减(?:去)?基线值|以随访减基线为变化量",
     r"按随机组分析|疗效仍归入原随机组",r"保留停药及救援后测量|停药或使用救援治疗后取得的评分仍予保留",
     r"基线必须完整|要求全部随机受试者有基线评分"]),
 ("efficacy","comparison"): ("比较{test}减{ref}的终点变化量，报告双侧{model_conf}%置信区间及P值",[
     r"组间效应按(?P<test>[^，。]+?)减(?P<ref>[^，。]+?)计算",r"给出双侧(?P<model_conf>\d+(?:\.\d+)?)%置信区间及P值"]),
 ("efficacy","ancova"): ("{ancova_role}分析采用ANCOVA，终点观察病例按治疗组和基线拟合普通最小二乘模型，采用残差自由度的t分布",[
     r"(?P<ancova_role>主要|支持性|敏感性)比较采用协方差分析(?:[(]ANCOVA[)])?",r"仅纳入有末访实测值的病例",
     r"用普通最小二乘法拟合治疗组及基线(?:评分)?",r"推断使用残差自由度的t分布"]),
 ("safety","window"): ("TEAE观察窗为首次实际用药日至末次实际用药后{follow}日，含首尾两日",[
     r"TEAE从首次(?:服药|给药)当天开始观察",r"截至末次(?:服药|给药)后第(?P<follow>\d+)天",r"起止日均计入"]),
 ("safety","emergence"): ("窗内新发事件及首次用药前已发生而在窗内加重的事件计为TEAE",[
     r"将窗内新发事件及用药前已出现但在窗内加重的事件归为TEAE"]),
 ("safety","counts"): ("按实际治疗组报告发生TEAE的人数、百分比和事件数，同一人同一首选术语只计一人",[
     r"按实际治疗组汇总TEAE发生人数、百分比和事件件数",r"同一受试者多次发生相同首选术语时人数只计一次"]),
 ("safety","categories"): ("按研究者记录汇总严重、致死、药物相关和{grade}级及以上TEAE，相关性待定者单列",[
     r"依据研究者评价分列严重、致死、药物相关及(?P<grade>[1-5])级以上[(]含本级[)]TEAE",r"尚未判定相关性的个例单列"]),
 ("safety","related_terms"): ("相关性为{related_terms}计入药物相关，无关不计入，待评估单列",[
     r"药物相关包括(?P<related_terms>[^，。]+)",r"无关事件不纳入相关事件",r"待评估另列"]),
 ("safety","exposure"): ("暴露天数按每人实际正剂量给药的不同日期计数，报告组内总人日",[
     r"每人每个实际正剂量给药日记一天",r"同日多次给药仅计一天",r"按实际组累加为总用药人日"]),
 ("descriptors","lab"): ("安全集按实际组汇总{lab_code}在BL和{lab_visit}的观察人数、均值及配对变化，临床重要异常按研究者标记逐人计数",[
     r"在安全集中按实际治疗组描述实验室指标(?P<lab_code>[A-Za-z0-9_]+)在BL和(?P<lab_visit>[A-Za-z0-9_]+)的例数、均值和配对变化",r"临床重要异常依据研究者标记按人计数"]),
 ("descriptors","vital"): ("安全集按实际组汇总{vs_code}在BL和{vs_visit}的观察人数、均值及配对变化",[
     r"在安全集中按实际治疗组描述生命体征(?P<vs_code>[A-Za-z0-9_]+)在BL和(?P<vs_visit>[A-Za-z0-9_]+)的例数、均值和配对变化"])
}

def compile_prose(section, clauses, normalize, strict):
    hits=defaultdict(dict)
    candidates=[]
    originals=[]
    for sentence,loc in clauses:
        # Prefer established, completely recognized clauses.
        if any(re.fullmatch(rx,normalize(sentence),re.I) for rx in strict.values()):
            originals.append((sentence,loc))
            continue
        spans=[s.strip() for s in re.split(r"[，,]",sentence) if s.strip()]
        parsed=[]
        for span in spans:
            matches=[(key,i,m.groupdict()) for key,(_,parts) in FORMS.items() if key[0]==section
                     for i,rx in enumerate(parts) if (m:=re.fullmatch(rx,normalize(span),re.I))]
            if len(matches)!=1:
                break
            parsed.append(matches[0])
        if not parsed or len(parsed)!=len(spans):
            originals.append((sentence,loc))
            continue
        for key,i,vals in parsed:
            hits[key].setdefault(i,[]).append((vals,sentence,loc))
        candidates.append((sentence,loc,parsed))
    compiled=[];proof={}
    for key,parts in hits.items():
        template,rules=FORMS[key]
        if set(parts)!=set(range(len(rules))):
            continue
        flat=[item for group in parts.values() for item in group]
        if any(len({tuple(sorted(x[0].items())) for x in grp})>1 for grp in parts.values()):
            continue
        params={k:v for vals,_,_ in flat for k,v in vals.items()}
        canonical=template.format(**params)
        locations=list(dict.fromkeys((q,l) for _,q,l in flat))
        proof[key[1]]=[dict(quote=q,location=l) for q,l in locations]
        compiled.append((canonical,locations[0][1]))
    originals.extend((s,l) for s,l,items in candidates if any(k[1] not in proof for k,_,_ in items))
    return originals+compiled,proof
