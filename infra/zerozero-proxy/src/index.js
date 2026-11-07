const ALLOWED_HOSTS=new Set(["www.zerozero.pt","zerozero.pt","www.zerozero.africa","zerozero.africa","www.zerozero.dk","zerozero.dk","www.zerozero.football","zerozero.football","www.zerozero.gr","zerozero.gr"]);
const MIRROR_HOSTS=["www.zerozero.pt","www.zerozero.africa","www.zerozero.dk","www.zerozero.football","www.zerozero.gr"];
const corsHeaders=()=>({"Access-Control-Allow-Origin":"*","Access-Control-Allow-Methods":"GET, OPTIONS","Access-Control-Allow-Headers":"Content-Type","Cache-Control":"no-store, no-cache, must-revalidate"});
const json=(body,status=200)=>new Response(JSON.stringify(body),{status,headers:{...corsHeaders(),"Content-Type":"application/json;charset=UTF-8"}});
const validArticlePath=p=>p.startsWith("/noticias/")&&p.length>12;

function decodeEntities(s){
  return String(s||"")
    .replace(/&nbsp;/gi," ")
    .replace(/&amp;/gi,"&").replace(/&quot;/gi,'"').replace(/&#39;|&apos;/gi,"'")
    .replace(/&lt;/gi,"<").replace(/&gt;/gi,">")
    .replace(/&#(\d+);/g,(_,n)=>String.fromCodePoint(Number(n)))
    .replace(/&#x([0-9a-f]+);/gi,(_,n)=>String.fromCodePoint(parseInt(n,16)));
}

function cleanText(html){
  return decodeEntities(String(html||"")
    .replace(/<script[\s\S]*?<\/script>/gi," ")
    .replace(/<style[\s\S]*?<\/style>/gi," ")
    .replace(/<noscript[\s\S]*?<\/noscript>/gi," ")
    .replace(/<br\s*\/?>(?=\s*)/gi,"\n")
    .replace(/<\/p\s*>/gi,"\n")
    .replace(/<\/h[1-6]\s*>/gi,"\n")
    .replace(/<[^>]+>/g," "))
    .replace(/\r/g,"")
    .split(/\n/).map(x=>x.replace(/\s+/g," ").trim())
    .filter(x=>x.length>=20)
    .join("\n\n").trim();
}

function escapeHtml(s){
  return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}

function textToHtml(text){
  return String(text||"").split(/\n{2,}/).map(p=>p.trim()).filter(Boolean).map(p=>"<p>"+escapeHtml(p)+"</p>").join("\n");
}

function markdownToHtml(md){
  const esc=s=>escapeHtml(s);
  const out=[];
  for(const raw of String(md||"").split(/\r?\n/)){
    const line=raw.trim();
    if(!line)continue;
    if(/^!\[[^\]]*\]\([^)]*\)$/.test(line))continue;
    if(/^#{1,6}\s+/.test(line)){
      const m=line.match(/^(#{1,6})\s+(.*)$/);
      const level=Math.min(m[1].length,3);
      out.push("<h"+level+">"+esc(m[2])+"</h"+level+">");
      continue;
    }
    if(/^[-*]\s+/.test(line)){out.push("<p>"+esc(line.replace(/^[-*]\s+/,""))+"</p>");continue;}
    if(/^(menu|pesquisar|publicidade|partilhar|comentários?|comments?|notícias?|futebol|última hora)$/i.test(line))continue;
    const clean=line.replace(/\[([^\]]+)\]\(https?:\/\/[^)]+\)/g,"$1");
    if(clean.length>=25)out.push("<p>"+esc(clean)+"</p>");
  }
  return out.join("\n");
}

function extractJsonLd(html){
  const scripts=[...String(html||"").matchAll(/<script[^>]+type=["']application\/ld\+json["'][^>]*>([\s\S]*?)<\/script>/gi)];
  for(const m of scripts){
    try{
      const root=JSON.parse(m[1].trim());
      const nodes=Array.isArray(root)?root:(Array.isArray(root?.["@graph"])?root["@graph"]:[root]);
      for(const n of nodes){
        const body=n?.articleBody||n?.text;
        if(typeof body==="string"&&body.trim().length>=250)return body.trim();
      }
    }catch{}
  }
  return "";
}

function extractArticle(html){
  const raw=String(html||"");
  const jsonBody=extractJsonLd(raw);
  if(jsonBody.length>=250)return {text:jsonBody,html:textToHtml(jsonBody),method:"jsonld"};

  const articleMatches=[...raw.matchAll(/<article\b[^>]*>([\s\S]*?)<\/article>/gi)];
  for(const m of articleMatches){
    const text=cleanText(m[1]);
    if(text.length>=250)return {text,html:m[1],method:"article"};
  }

  const mainMatches=[...raw.matchAll(/<main\b[^>]*>([\s\S]*?)<\/main>/gi)];
  for(const m of mainMatches){
    const text=cleanText(m[1]);
    if(text.length>=300)return {text,html:m[1],method:"main"};
  }

  const paragraphs=[...raw.matchAll(/<p\b[^>]*>([\s\S]*?)<\/p>/gi)]
    .map(m=>cleanText(m[1]))
    .filter(p=>p.length>=25)
    .filter(p=>!/(^|\s)(menu|pesquisar|publicidade|partilhar|comentários|comments)(\s|$)/i.test(p));
  const unique=[];
  for(const p of paragraphs)if(!unique.includes(p))unique.push(p);
  const text=unique.join("\n\n");
  if(text.length>=250)return {text,html:textToHtml(text),method:"paragraphs"};
  return null;
}

function responseLooksUseful(body){
  if(!body||body.length<250)return false;
  const low=body.slice(0,5000).toLowerCase();
  if(/cf-chl-|just a moment|attention required|captcha|access denied/i.test(low))return false;
  return /<p\b|<article\b|<main\b|application\/ld\+json|og:description/i.test(low);
}


// ---------------------------------------------------------------------------
// /live — estado em direto do jogo do Sporting para o cartão "A decorrer".
// Fonte principal: SofaScore (feed da equipa + incidentes). Fallback: FotMob
// matchDetails com o id do jogo (?fotmob=ID). Resposta sempre com CORS.
// ---------------------------------------------------------------------------
const SOFA_TEAM_ID=3001;
const UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36";
async function getJson(url,referer){
  const r=await fetch(url,{headers:{"User-Agent":UA,"Accept":"application/json,text/plain,*/*","Referer":referer,"Origin":new URL(referer).origin},cf:{cacheTtl:0,cacheEverything:false}});
  if(!r.ok)throw new Error(url+" HTTP "+r.status);
  return r.json();
}
function sofaMinute(e,nowSec){
  const code=Number(e.status?.code),start=Number(e.time?.currentPeriodStartTimestamp||0);
  const base={6:0,7:45,41:90,42:105}[code];
  if(base==null||!start)return null;
  const cap={6:45,7:90,41:105,42:120}[code];
  const m=base+Math.max(1,Math.floor((nowSec-start)/60)+1);
  return m>cap?cap+"+"+(m-cap)+"'":m+"'";
}
async function liveFromSofa(){
  const nowSec=Math.floor(Date.now()/1000);
  const ref="https://www.sofascore.com/";
  const feeds=await Promise.allSettled(["last","next"].map(d=>getJson(`https://api.sofascore.com/api/v1/team/${SOFA_TEAM_ID}/events/${d}/0`,ref)));
  const ok=feeds.filter(f=>f.status==="fulfilled");
  if(!ok.length)throw new Error("sofascore feed unavailable");
  const events=ok.flatMap(f=>f.value.events||[]);
  // jogo em curso, ou terminado há menos de 3h (para o cliente saber que acabou)
  const ev=events.find(e=>String(e.status?.type).toLowerCase()==="inprogress")
        ||events.filter(e=>String(e.status?.type).toLowerCase()==="finished"&&nowSec-Number(e.startTimestamp||0)<4*3600).sort((a,b)=>b.startTimestamp-a.startTimestamp)[0];
  if(!ev)return {state:"none",source:"sofascore"};
  const live=String(ev.status?.type).toLowerCase()==="inprogress";
  let scorers=[];
  try{
    const inc=await getJson(`https://api.sofascore.com/api/v1/event/${ev.id}/incidents`,ref);
    scorers=(inc.incidents||[]).filter(i=>i.incidentType==="goal").map(i=>({
      side:i.isHome?"home":"away",
      player:i.player?.shortName||i.player?.name||i.playerName||"",
      minute:(i.time!=null?i.time:"")+(i.addedTime?"+"+i.addedTime:"")+"'",
      type:i.incidentClass==="ownGoal"?"og":(i.incidentClass==="penalty"?"pen":"")
    })).sort((a,b)=>parseInt(a.minute)-parseInt(b.minute));
  }catch(e){}
  const code=Number(ev.status?.code);
  return {
    state:live?"live":"finished",source:"sofascore",
    kickoff:Number(ev.startTimestamp||0),
    home:{name:ev.homeTeam?.name||"",id:ev.homeTeam?.id},
    away:{name:ev.awayTeam?.name||"",id:ev.awayTeam?.id},
    score:{home:ev.homeScore?.current??0,away:ev.awayScore?.current??0},
    clock:live?(code===31?"Intervalo":(sofaMinute(ev,nowSec)||ev.status?.description||"Em direto")):"Terminado",
    status:ev.status?.description||"",
    scorers
  };
}
async function liveFromFotmob(id){
  const d=await getJson(`https://www.fotmob.com/api/data/matchDetails?matchId=${encodeURIComponent(id)}`,"https://www.fotmob.com/");
  const h=d.header||{},st=h.status||d.general||{},teams=h.teams||[];
  const started=!!(st.started??d.general?.started),finished=!!(st.finished??d.general?.finished);
  if(!started)return {state:"none",source:"fotmob"};
  const goals=(side,obj)=>Object.entries(obj||{}).flatMap(([name,list])=>(Array.isArray(list)?list:[]).map(g=>({
    side,player:g.player?.name||g.nameStr||name,
    minute:(g.time??g.timeStr??"")+(g.overloadTime?"+"+g.overloadTime:"")+"'",
    type:g.ownGoal?"og":(/penal/i.test(g.goalDescription||g.suffix||"")?"pen":"")})));
  const ev=h.events||{};
  const scorers=[...goals("home",ev.homeTeamGoals),...goals("away",ev.awayTeamGoals)].sort((a,b)=>parseInt(a.minute)-parseInt(b.minute));
  const lt=st.liveTime?.short||st.liveTime?.long||"";
  return {
    state:finished?"finished":"live",source:"fotmob",
    home:{name:teams[0]?.name||"",id:teams[0]?.id},away:{name:teams[1]?.name||"",id:teams[1]?.id},
    score:{home:teams[0]?.score??0,away:teams[1]?.score??0},
    clock:finished?"Terminado":(/^HT$|half/i.test(st.reason?.short||lt)?"Intervalo":(lt||"Em direto")),
    status:st.reason?.long||"",scorers
  };
}
async function handleLive(url){
  const fotmobId=url.searchParams.get("fotmob");
  const errors=[];
  try{const r=await liveFromSofa();if(r.state!=="none"||!fotmobId)return json({ok:true,...r});}catch(e){errors.push(String(e.message||e));}
  if(fotmobId&&/^\d+$/.test(fotmobId)){
    try{return json({ok:true,...(await liveFromFotmob(fotmobId))});}catch(e){errors.push(String(e.message||e));}
  }
  return json({ok:false,error:"live_unavailable",detail:errors},502);
}

export default{async fetch(request){
  const headers=corsHeaders();
  if(request.method==="OPTIONS")return new Response(null,{headers});
  if(request.method!=="GET")return json({error:"method_not_allowed"},405);
  {const u=new URL(request.url);if(u.pathname==="/live")return handleLive(u);}

  const target=new URL(request.url).searchParams.get("url");
  if(!target)return json({error:"missing_url"},400);

  let parsed;
  try{parsed=new URL(target)}catch{return json({error:"invalid_url"},400);}
  if(!ALLOWED_HOSTS.has(parsed.hostname)||!validArticlePath(parsed.pathname))return json({error:"url_not_allowed"},403);

  const requestHeaders={
    "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.7",
    "Accept-Language":"pt-PT,pt;q=0.9,en;q=0.7",
    "Cache-Control":"no-cache",
    "Pragma":"no-cache",
    "Referer":"https://www.zerozero.pt/",
    "Sec-Fetch-Dest":"document",
    "Sec-Fetch-Mode":"navigate",
    "Sec-Fetch-Site":"same-origin"
  };

  const candidates=[];
  const add=u=>{if(u&&!candidates.includes(u))candidates.push(u)};
  add(target);
  for(const host of MIRROR_HOSTS)add(new URL(parsed.pathname+parsed.search,"https://"+host).toString());
  for(const host of MIRROR_HOSTS){
    add("https://r.jina.ai/https://"+host+parsed.pathname+parsed.search);
    add("https://r.jina.ai/http://"+host+parsed.pathname+parsed.search);
  }

  let lastStatus=502,lastError="";

  for(const candidate of candidates){
    try{
      const isJina=new URL(candidate).hostname==="r.jina.ai";
      const response=await fetch(candidate,{
        headers:isJina?{"User-Agent":"Mozilla/5.0","Accept":"text/plain,text/markdown;q=0.9,*/*;q=0.8"}:requestHeaders,
        redirect:"follow",
        cf:{cacheTtl:0,cacheEverything:false}
      });
      lastStatus=response.status;
      const body=await response.text();
      if(!response.ok){lastError="HTTP "+response.status;continue;}

      let extracted=null;
      if(isJina){
        const html=markdownToHtml(body);
        extracted=extractArticle(html);
        if(!extracted&&body.length>=250)extracted={text:cleanText(html),html,method:"jina-fallback"};
      }else if(responseLooksUseful(body)){
        extracted=extractArticle(body);
        if(!extracted){
          const text=cleanText(body);
          if(text.length>=250)extracted={text,html:textToHtml(text),method:"fullpage-text"};
        }
      }

      if(extracted&&extracted.text.length>=250){
        return json({
          ok:true,
          format:"article",
          source:new URL(candidate).hostname,
          requested_url:target,
          article_text:extracted.text,
          html:extracted.html,
          extraction:extracted.method
        });
      }
      lastError="response_not_useful_or_article_not_found";
    }catch(e){lastError=String(e?.message||e);}
  }

  return json({ok:false,error:"zerozero_unavailable",status:lastStatus,detail:lastError},502);
}};