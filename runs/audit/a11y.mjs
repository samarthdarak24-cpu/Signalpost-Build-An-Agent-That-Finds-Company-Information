/* Accessibility and semantics audit over the generated markup. */
import { readFileSync } from "fs";
const BASE="http://127.0.0.1:8787";
const el=()=>({tagName:"DIV",children:[],attrs:{},dataset:{},style:{},_html:"",hidden:false,
 classList:{_s:new Set(),add(...c){c.forEach(x=>this._s.add(x))},remove(...c){c.forEach(x=>this._s.delete(x))},contains(c){return this._s.has(c)}},
 get innerHTML(){return this._html},set innerHTML(v){this._html=String(v)},textContent:"",
 querySelector(s){const id=String(s).match(/^#([\w-]+)$/),cl=String(s).match(/^\.([\w-]+)$/);
  const re=id?new RegExp(`id="${id[1]}"`):(cl?new RegExp(`class="[^"]*\\b${cl[1]}\\b`):null);
  return (re&&re.test(this._html))?el():null},
 querySelectorAll(s){const o=[];const cl=String(s).match(/^\.([\w-]+)$/);
  if(cl){const n=(this._html.match(new RegExp(`class="[^"]*\\b${cl[1]}\\b`,"g"))||[]).length;for(let i=0;i<n;i++)o.push(el())}return o},
 addEventListener(){},appendChild(c){this.children.push(c);return c},remove(){},setAttribute(){},getAttribute(){return null},
 removeAttribute(){},insertAdjacentHTML(){},scrollIntoView(){},focus(){},blur(){},closest(){return null},contains(){return false},
 lastElementChild:null,offsetWidth:1,value:"",removeChild(){},insertBefore(){},replaceChild(){}});
const reg=new Map(); const getEl=i=>{if(!reg.has(i))reg.set(i,el());return reg.get(i)};
const w=globalThis; w.addEventListener=()=>{}; w.removeEventListener=()=>{}; w.__lastFocus=null; globalThis.window=w;
globalThis.document={documentElement:el(),body:el(),title:"",querySelector:s=>getEl(String(s).replace(/^#/,"")),
 querySelectorAll:()=>[],createElement:()=>el(),addEventListener:()=>{},activeElement:null};
globalThis.location={hash:"#/overview",href:BASE};
globalThis.localStorage={_d:{},getItem(k){return this._d[k]??null},setItem(k,v){this._d[k]=v}};
globalThis.matchMedia=()=>({matches:false,addEventListener(){}});
globalThis.requestAnimationFrame=f=>setTimeout(f,0);
const rf=globalThis.fetch; globalThis.fetch=(p,o)=>rf(String(p).startsWith("http")?p:BASE+p,o);
const src=readFileSync("web-next/app.js","utf8");
const V=new Function(src+";return {viewOverview,viewSearch,viewCompany,viewCoverage,viewTrace,viewAsk,viewReports,viewSources,viewAuditor,viewLive};")();
await new Promise(r=>setTimeout(r,900));

const views={overview:await V.viewOverview(),search:await V.viewSearch(),
 company:await V.viewCompany("923609016","overview"),coverage:await V.viewCoverage(),
 trace:await V.viewTrace("923609016"),ask:await V.viewAsk(),reports:await V.viewReports(),
 sources:await V.viewSources(),auditor:await V.viewAuditor(),live:await V.viewLive()};

let issues=0;
const report=(v,msg)=>{console.log(`  [${v}] ${msg}`);issues++;};

for (const [name,html] of Object.entries(views)) {
  // inputs must have a label, aria-label or be inside a labelled form
  for (const m of html.matchAll(/<input\b([^>]*)>/g)) {
    const a=m[1];
    if (!/aria-label=|type="hidden"|id="[^"]+"\s[^>]*aria-labelledby|list=/.test(a)) {
      // check for an adjacent <label for="id">
      const id=(a.match(/id="([^"]+)"/)||[])[1];
      if (!id || !new RegExp(`<label[^>]*for="${id}"`).test(html)) report(name,`input without label: ${a.slice(0,80)}`);
    }
  }
  // buttons must have text or aria-label
  for (const m of html.matchAll(/<button\b([^>]*)>([\s\S]*?)<\/button>/g)) {
    const [,attrs,inner]=m;
    const hasText=inner.replace(/<[^>]+>/g,"").trim().length>0;
    const hasAria=/aria-label=/.test(attrs);
    if (!hasText && !hasAria) report(name,`button without accessible name: ${attrs.slice(0,70)}`);
  }
  // interactive rows need a keyboard affordance
  const clickable=(html.match(/class="clickable[^"]*"/g)||[]).length;
  const tabindex=(html.match(/class="clickable[^"]*"[^>]*tabindex="0"/g)||[]).length;
  if (clickable>0 && tabindex<clickable) report(name,`${clickable-tabindex} clickable row(s) missing tabindex`);
  // tables need a header
  if (/<table/.test(html) && !/<th\b/.test(html)) report(name,"table without <th>");
  // links need href text
  const emptyLinks=(html.match(/<a\b[^>]*>\s*<\/a>/g)||[]).length;
  if (emptyLinks) report(name,`${emptyLinks} link(s) with no text`);
  // images/aria
  if (/<svg/.test(html) && !/aria-hidden="true"/.test(html) && !/aria-label=/.test(html))
    report(name,"svg without aria-hidden or aria-label");
  console.log(`${name.padEnd(9)} ${String(html.length).padStart(6)} chars  inputs=${(html.match(/<input/g)||[]).length} buttons=${(html.match(/<button/g)||[]).length} rows=${clickable}`);
}
console.log(`\nissues: ${issues}`);
