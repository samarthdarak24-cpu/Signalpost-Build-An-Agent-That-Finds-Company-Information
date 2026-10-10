/* Exercise the Live Research turn rendering with a real agent answer. */
import { readFileSync } from "fs";
const BASE="http://127.0.0.1:8787";
function makeEl(tag="div"){return{tagName:tag,children:[],attrs:{},dataset:{},style:{},_html:"",hidden:false,
 classList:{_s:new Set(),add(...c){c.forEach(x=>this._s.add(x))},remove(...c){c.forEach(x=>this._s.delete(x))},contains(c){return this._s.has(c)}},
 get innerHTML(){return this._html},set innerHTML(v){this._html=String(v)},textContent:"",
 querySelector(s){const id=String(s).match(/^#([\w-]+)$/);const cl=String(s).match(/^\.([\w-]+)$/);
  const re=id?new RegExp(`id="${id[1]}"`):(cl?new RegExp(`class="[^"]*\\b${cl[1]}\\b`):null);
  return (re&&re.test(this._html))?makeEl("div"):null},
 querySelectorAll(s){const o=[];const cl=String(s).match(/^\.([\w-]+)$/);
  if(cl){const n=(this._html.match(new RegExp(`class="[^"]*\\b${cl[1]}\\b`,"g"))||[]).length;for(let i=0;i<n;i++)o.push(makeEl("div"))}return out||o} ,
 addEventListener(){},appendChild(c){this.children.push(c);return c},remove(){},setAttribute(){},getAttribute(){return null},
 removeAttribute(){},insertAdjacentHTML(){},scrollIntoView(){},focus(){},blur(){},closest(){return null},contains(){return false},
 lastElementChild:null,offsetWidth:1,value:"",removeChild(){},insertBefore(){},replaceChild(){}}}
const reg=new Map();
const getEl=id=>{if(!reg.has(id))reg.set(id,makeEl(id==="main"?"main":"div"));return reg.get(id)};
const win=globalThis; win.addEventListener=()=>{}; win.removeEventListener=()=>{}; win.__lastFocus=null;
globalThis.window=win;
globalThis.document={documentElement:makeEl("html"),body:makeEl("body"),title:"",
 querySelector:s=>getEl(String(s).replace(/^#/,"")),querySelectorAll:()=>[],createElement:t=>makeEl(t),
 addEventListener:()=>{},activeElement:null};
globalThis.location={hash:"#/live",href:BASE};
globalThis.localStorage={_d:{},getItem(k){return this._d[k]??null},setItem(k,v){this._d[k]=v}};
globalThis.matchMedia=()=>({matches:false,addEventListener(){}});
globalThis.requestAnimationFrame=f=>setTimeout(f,0);
const rf=globalThis.fetch; globalThis.fetch=(p,o)=>rf(String(p).startsWith("http")?p:BASE+p,o);
const src=readFileSync("web-next/app.js","utf8");
const V=new Function(src+";return {viewLive,liveTurn,liveState,liveSteps};")();
await new Promise(r=>setTimeout(r,900));

// 1. the loading state
const loading = V.liveTurn({q:"923609016", loading:true}, 0);
console.log("loading state has progress:", /progress/.test(loading), "| spinner:", /spinner/.test(loading),
            "| claims stages server-side:", /stages run server-side/.test(loading));

// 2. an error state
const err = V.liveTurn({q:"x", error:"HTTP 500"}, 0);
console.log("error state:", /request failed/.test(err) ? "OK" : "MISSING");

// 3. a REAL answer from the agent
const res = await fetch(BASE+"/api/live", {method:"POST",
  headers:{"Content-Type":"application/json"}, body:JSON.stringify({q:"923609016", session:"audit-x"})});
const answer = await res.json();
const turn = V.liveTurn({q:"923609016", answer}, 0);
console.log("\n--- real answer ---");
console.log("kind:", answer.kind, "| answered:", answer.answered, "| seconds:", answer.seconds);
console.log("target:", answer.target, "| sources:", (answer.sources||[]).length, "| evidence:", (answer.evidence||[]).length);
const text = turn.replace(/<[^>]+>/g," ").replace(/\s+/g," ").trim();
console.log("\nrendered turn:");
console.log(text.slice(0,1100));
console.log("\nhas source cards:", /class="src"/.test(turn));
console.log("has evidence buttons:", /data-evidence=/.test(turn));
console.log("has undefined leak:", /undefined/.test(turn));
