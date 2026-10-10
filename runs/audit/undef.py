import re, sys
src = open(sys.argv[1], encoding='utf-8').read()

def strip_code(s):
    out=[]; i=0; n=len(s)
    while i<n:
        if s.startswith('/*',i):
            j=s.find('*/',i+2); i=(j+2) if j!=-1 else n; continue
        if s.startswith('//',i):
            j=s.find('\n',i); i=(j+1) if j!=-1 else n; continue
        c=s[i]
        if c=='`':
            i+=1
            while i<n and s[i]!='`':
                if s.startswith('${',i):
                    depth=1; i+=2; st=i
                    while i<n and depth:
                        if s[i]=='{': depth+=1
                        elif s[i]=='}': depth-=1
                        i+=1
                    out.append(' '+s[st:i-1]+' ')
                else: i+=1
            i+=1; continue
        if c in '"\'':
            q=c; i+=1
            while i<n and s[i]!=q:
                if s[i]=='\\': i+=1
                i+=1
            i+=1; out.append(' "" '); continue
        out.append(c); i+=1
    return ''.join(out)

code=strip_code(src)
declared=set()
for pat in [r'\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)', r'\bfunction\s+([A-Za-z_$][\w$]*)']:
    for m in re.finditer(pat, code): declared.add(m.group(1))
for m in re.finditer(r'\b(?:const|let|var)\s*\{([^}]*)\}', code):
    for p in m.group(1).split(','):
        nm=p.split(':')[-1].split('=')[0].strip()
        if re.match(r'^[A-Za-z_$][\w$]*$', nm): declared.add(nm)
for m in re.finditer(r'\b(?:const|let|var)\s*\[([^\]]*)\]', code):
    for p in m.group(1).split(','):
        nm=p.split('=')[0].strip()
        if re.match(r'^[A-Za-z_$][\w$]*$', nm): declared.add(nm)
for m in re.finditer(r'function\s*[A-Za-z_$\w]*\s*\(([^)]*)\)', code):
    for p in m.group(1).split(','):
        nm=p.split('=')[0].strip().lstrip('{[').rstrip('}]')
        if re.match(r'^[A-Za-z_$][\w$]*$', nm): declared.add(nm)
for m in re.finditer(r'\(([^)]*)\)\s*=>', code):
    for p in m.group(1).split(','):
        nm=p.split('=')[0].strip().lstrip('{[').rstrip('}]')
        if re.match(r'^[A-Za-z_$][\w$]*$', nm): declared.add(nm)
for m in re.finditer(r'\b([A-Za-z_$][\w$]*)\s*=>', code): declared.add(m.group(1))
for m in re.finditer(r'catch\s*\(\s*([A-Za-z_$][\w$]*)', code): declared.add(m.group(1))

kw=set('''Array Boolean Date Error JSON Math Number Object Promise RegExp Set String URL Map WeakMap
document window location history localStorage matchMedia fetch setTimeout clearTimeout setInterval
clearInterval console isNaN isFinite parseInt parseFloat encodeURIComponent decodeURIComponent
undefined NaN Infinity globalThis requestAnimationFrame this new typeof instanceof delete void in
of return if else for while do switch case break continue throw try catch finally class extends super
yield await async function const let var true false null get set static import export default'''.split())

props=set(re.findall(r'[.]([A-Za-z_$][\w$]*)', code))
keys=set(re.findall(r'([A-Za-z_$][\w$]*)\s*:', code))
used=set(re.findall(r'(?<![\w.$])([A-Za-z_$][\w$]*)', code))
unknown=sorted(u for u in used if u not in declared and u not in kw and u not in props and u not in keys)
print('=== referenced but not declared ===')
for u in unknown:
    line=''
    for l in code.split('\n'):
        if re.search(r'(?<![\w.$])'+re.escape(u)+r'\b', l): line=l.strip()[:100]; break
    print(f'  {u:24s} {line}')
if not unknown: print('  none')
