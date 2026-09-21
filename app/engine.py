"""Small deterministic rules engine. No eval(), code generation, or task scheduler."""
import ast
import math
import re

SEVERITIES = {'high', 'medium', 'normal', 'unknown'}
TEMPLATES = {'operations', 'portfolio', 'quality'}
COMPONENTS = {'goals','pipeline','trend','distribution','workload','table','activity'}
FUNCS = {'min': min, 'max': max, 'round': round, 'abs': abs}

class DomainError(Exception):
    def __init__(self, message, status=400, details=None):
        super().__init__(message)
        self.status, self.details = status, details

def expression(text, values):
    if not isinstance(text, str) or len(text) > 1200:
        raise ValueError('表达式必须是 1,200 字符以内的文本')
    tree = ast.parse(text, mode='eval')
    if len(list(ast.walk(tree))) > 150:
        raise ValueError('表达式过于复杂')
    def walk(n):
        if isinstance(n, ast.Expression): return walk(n.body)
        if isinstance(n, ast.Constant):
            if isinstance(n.value, (int, float, str, bool)) or n.value is None: return n.value
        if isinstance(n, ast.Name):
            if n.id not in values: raise ValueError('未定义字段：' + n.id)
            return values[n.id]
        if isinstance(n, ast.BinOp):
            a,b=walk(n.left),walk(n.right)
            if not isinstance(a,(int,float)) or not isinstance(b,(int,float)): raise ValueError('算术运算需要数字')
            if isinstance(n.op, ast.Add): return a+b
            if isinstance(n.op, ast.Sub): return a-b
            if isinstance(n.op, ast.Mult): return a*b
            if isinstance(n.op, ast.Div): return a/b
            if isinstance(n.op, ast.Mod): return a%b
        if isinstance(n, ast.UnaryOp):
            a=walk(n.operand)
            if isinstance(n.op, ast.Not): return not a
            if isinstance(n.op, ast.USub): return -a
            if isinstance(n.op, ast.UAdd): return +a
        if isinstance(n, ast.BoolOp):
            if isinstance(n.op, ast.And):
                for v in n.values:
                    if not walk(v): return False
                return True
            if isinstance(n.op, ast.Or):
                for v in n.values:
                    if walk(v): return True
                return False
        if isinstance(n, ast.Compare):
            a=walk(n.left)
            for op,right in zip(n.ops,n.comparators):
                b=walk(right)
                if isinstance(op,ast.Eq): ok=a==b
                elif isinstance(op,ast.NotEq): ok=a!=b
                elif isinstance(op,ast.Gt): ok=a>b
                elif isinstance(op,ast.GtE): ok=a>=b
                elif isinstance(op,ast.Lt): ok=a<b
                elif isinstance(op,ast.LtE): ok=a<=b
                else: raise ValueError('不支持的比较运算')
                if not ok: return False
                a=b
            return True
        if isinstance(n,ast.IfExp): return walk(n.body if walk(n.test) else n.orelse)
        if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id in FUNCS and not n.keywords:
            return FUNCS[n.func.id](*[walk(a) for a in n.args])
        raise ValueError('不支持的表达式结构：'+type(n).__name__)
    result=walk(tree)
    if isinstance(result,float) and not math.isfinite(result): raise ValueError('计算结果不是有限数值')
    return result

def names_in(expr):
    t=ast.parse(expr,mode='eval')
    return {n.id for n in ast.walk(t) if isinstance(n,ast.Name)} - set(FUNCS)

def format_text(text, values):
    return re.sub(r'\{([a-zA-Z_][a-zA-Z_0-9]*)\}',lambda m: str(values.get(m.group(1), '—')),str(text))

def evaluate(pack, record, parameters=None):
    """Map source fields, derive fields in declared order, then first-match rules."""
    values=dict(record)
    for target, source in pack.get('mapping',{}).items(): values[target]=record.get(source)
    params={**pack.get('parameters',{}),**(parameters or {})}
    trace=[]; errors=[]
    for f in pack.get('fields',[]):
        val=values.get(f['key'])
        if f.get('required') and (val is None or val==''): errors.append(f['label']+'缺失')
        elif val is not None and f.get('type')=='number':
            if isinstance(val,bool): errors.append(f['label']+'必须是数字')
            else:
                try:
                    val=float(val)
                    if not math.isfinite(val): raise ValueError()
                    values[f['key']]=int(val) if val.is_integer() else val
                except (TypeError,ValueError): errors.append(f['label']+'不是有效数字')
    if errors:
        return {**values,'severity':'unknown','rule_id':'DATA-QUALITY','reason':'；'.join(errors), 'trace':[{'stage':'data_contract','status':'unknown','detail':errors}], 'impact':0}
    env={**values,**params}
    try:
        for f in pack.get('derived',[]):
            result=expression(f['expr'],env); env[f['key']]=result; values[f['key']]=result
            trace.append({'stage':'derive','field':f['key'],'expression':f['expr'],'result':result})
        for rule in pack.get('rules',[]):
            hit=bool(expression(rule['when'],env))
            trace.append({'stage':'rule','id':rule['id'],'name':rule['name'],'expression':rule['when'],'matched':hit})
            if hit:
                return {**values,'severity':rule['severity'],'rule_id':rule['id'],'reason':format_text(rule.get('reason',rule['name']),env),'trace':trace}
        return {**values,'severity':'unknown','rule_id':'NO-MATCH','reason':'未命中任何规则，请补充兜底规则。','trace':trace}
    except Exception as e:
        return {**values,'severity':'unknown','rule_id':'CALC-ERROR','reason':'规则计算失败：'+str(e),'trace':trace+[{'stage':'error','detail':str(e)}], 'impact':0}

def validate_pack(pack):
    errors=[]
    if not isinstance(pack,dict): return {'ok':False,'errors':['场景包必须是 JSON 对象'],'tests':[]}
    for key in ['schema_version','id','name','fields','rules','columns','acceptance']:
        if key not in pack: errors.append('缺少 '+key)
    if pack.get('schema_version')!='1.0': errors.append('仅支持 schema_version=1.0')
    if not re.fullmatch(r'[a-z][a-z0-9_-]{1,47}',str(pack.get('id',''))): errors.append('id 需为 2–48 位小写字母、数字、连字符或下划线')
    if pack.get('template','operations') not in TEMPLATES: errors.append('未知工作台模板')
    if any(x not in COMPONENTS for x in pack.get('layout',[])): errors.append('layout 包含未注册组件')
    try:
        fields=pack.get('fields',[]); known=set(); param_names=set(pack.get('parameters',{}))
        for f in fields:
            if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]*',f['key']): errors.append('字段 key 非法')
            if f['key'] in known: errors.append('重复字段 '+f['key'])
            known.add(f['key'])
        if not {'id','name','owner','site'} <= known: errors.append('P0 对象合同需有 id/name/owner/site 字段')
        if known & param_names: errors.append('参数不能覆盖业务字段')
        for key,val in pack.get('parameters',{}).items():
            if not isinstance(val,(int,float)) or isinstance(val,bool) or not math.isfinite(val): errors.append('参数必须是有限数字：'+key)
        known |= param_names
        for f in pack.get('derived',[]):
            missing=names_in(f['expr'])-known
            if missing: errors.append(f"派生字段 {f['key']} 引用未定义或尚未计算字段：{sorted(missing)}")
            if f['key'] in known: errors.append('派生字段重复：'+f['key'])
            # Validate structure without relying on a branch reaching all expressions.
            for n in ast.walk(ast.parse(f['expr'],mode='eval')):
                if isinstance(n,(ast.Attribute,ast.Subscript,ast.Lambda,ast.ListComp,ast.Dict)): errors.append('不支持的公式结构')
                if isinstance(n,ast.Call) and (not isinstance(n.func,ast.Name) or n.func.id not in FUNCS): errors.append('不支持的函数调用')
            known.add(f['key'])
        rids=set()
        for rule in pack.get('rules',[]):
            if rule['id'] in rids: errors.append('重复规则编号')
            rids.add(rule['id'])
            if rule['severity'] not in SEVERITIES: errors.append('规则等级不合法')
            missing=names_in(rule['when'])-known
            if missing: errors.append(f"规则 {rule['id']} 引用未知字段：{sorted(missing)}")
            for n in ast.walk(ast.parse(rule['when'],mode='eval')):
                if isinstance(n,(ast.Attribute,ast.Subscript,ast.Lambda,ast.ListComp,ast.Dict)): errors.append('不支持的条件结构')
                if isinstance(n,ast.Call) and (not isinstance(n.func,ast.Name) or n.func.id not in FUNCS): errors.append('不支持的函数调用')
        if not pack.get('rules'): errors.append('至少需要一条规则')
        for c in pack.get('columns',[]):
            if c['key'] not in known: errors.append('表格引用未知字段：'+c['key'])
        for target,source in pack.get('mapping',{}).items():
            if target not in known or not isinstance(source,str): errors.append('字段映射不合法')
        if pack.get('case',{}).get('key','id')!='id': errors.append('P0 Case 粒度固定为映射后的 id，请在数据映射阶段生成所需业务主键')
        for name,action in pack.get('actions',{}).items():
            if not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_.-]{1,80}',name): errors.append('业务动作标识不合法：'+name)
            if action.get('executor','hermes_skill')!='hermes_skill': errors.append('外部操作仅支持 Hermes Skill 执行：'+name)
            if action.get('ui','notification') not in ('notification','query'): errors.append('未知业务操作呈现形态：'+name)
            if action.get('ui','notification')=='notification' and action.get('group_by','all') not in known|{'all'}: errors.append('通知分组引用未知业务字段：'+name)
        auto=pack.get('triggers',{}).get('auto_capability','email.send')
        if pack.get('triggers',{}).get('auto_prepare') and (auto not in pack.get('actions',{}) or pack['actions'][auto].get('ui','notification')!='notification'): errors.append('自动草稿能力未配置或不是通知类型')
    except (KeyError,TypeError,ValueError,SyntaxError) as e: errors.append('定义不完整或语法错误：'+str(e))
    tests=[]
    if not errors:
        for t in pack.get('acceptance',[]):
            from .processing import process
            try:
                cooked=process(pack,t.get('records',[t.get('record',{})]),sources=t.get('tables',[])); result=evaluate(pack,cooked[0]) if len(cooked)==1 else {}
            except Exception as ex:
                errors.append('业务处理验收失败：'+str(ex)); result={}
            exp=t.get('expected',{})
            ok=bool(exp) and all(result.get(k)==v for k,v in exp.items())
            tests.append({'name':t.get('name','未命名样例'),'ok':ok,'expected':exp,'actual':{k:result.get(k) for k in exp}})
        if not tests: errors.append('至少需要一条有预期结果的验收样例')
    return {'ok':not errors and all(t['ok'] for t in tests),'errors':errors,'tests':tests,'passed':sum(t['ok'] for t in tests),'total':len(tests)}
