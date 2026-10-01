const ALLOWED_HOSTS=new Set(["www.zerozero.pt","zerozero.pt","www.zerozero.africa","zerozero.africa","www.zerozero.dk","zerozero.dk","www.zerozero.football","zerozero.football","www.zerozero.gr","zerozero.gr"]);
const MIRROR_HOSTS=["www.zerozero.pt","www.zerozero.africa","www.zerozero.dk","www.zerozero.football","www.zerozero.gr"];
const corsHeaders=()=>({"Access-Control-Allow-Origin":"*","Access-Control-Allow-Methods":"GET, OPTIONS","Access-Control-Allow-Headers":"Content-Type","Cache-Control":"no-store, no-cache, must-revalidate"});
const json=(body,status=200)=>new Response(JSON.stringify(body),{status,headers:{...corsHeaders(),"Content-Type":"application/json;charset=UTF-8"}});
const validArticlePath=p=>p.startsWith("/noticias/")&&p.length>12;

function htmlSizeLooksUseful(html){
  if(!html||html.length<2500)return false;
  const low=html.toLowerCase();
  if(/cf-chl-|just a moment|attention required|cloudflare|access denied|captcha/i.test(low))return false;
  return /<article\b|articlebody|og:description|<main\b|<p[ >]/i.test(html);
}

function markdownToHtml(md){
  const esc=s=>String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
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
    if(/^[-*]\s+/.test(line)){
      out.push("<p>"+esc(line.replace(/^[-*]\s+/,""))+"</p>");
      continue;
    }
    if(/^(menu|pesquisar|publicidade|partilhar|comentários?|comments?)$/i.test(line))continue;
    const clean=line.replace(/\[([^\]]+)\]\((https?:\/\/[^)]+)\)/g,"$1");
    if(clean.length>=25)out.push("<p>"+esc(clean)+"</p>");
  }
  return out.join("\n");
}

export default{async fetch(request){
 const headers=corsHeaders();
 if(request.method==="OPTIONS")return new Response(null,{headers});
 if(request.method!=="GET")return json({error:"method_not_allowed"},405);
 const target=new URL(request.url).searchParams.get("url");
 if(!target)return json({error:"missing_url"},400);
 let parsed;
 try{parsed=new URL(target)}catch{return json({error:"invalid_url"},400);}
 if(!ALLOWED_HOSTS.has(parsed.hostname)||!validArticlePath(parsed.pathname))return json({error:"url_not_allowed"},403);

 const requestHeaders={
   "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
   "Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
   "Accept-Language":"pt-PT,pt;q=0.9,en;q=0.7",
   "Cache-Control":"no-cache",
   "Pragma":"no-cache"
 };

 const candidates=[];
 const add=u=>{if(u&&!candidates.includes(u))candidates.push(u)};
 add(target);
 for(const host of MIRROR_HOSTS)add(new URL(parsed.pathname+parsed.search,"https://"+host).toString());

 try{
   const host=parsed.hostname.replace(/\./g,"-");
   let tr="https://"+host+".translate.goog"+parsed.pathname;
   tr+=(parsed.search?"?"+parsed.search.slice(1)+"&":"?")+"_x_tr_sl=auto&_x_tr_tl=en&_x_tr_hl=en";
   add(tr);
 }catch{}

 let lastStatus=502,lastError="";

 for(const candidate of candidates){
   try{
     const response=await fetch(candidate,{headers:requestHeaders,redirect:"follow",cf:{cacheTtl:0,cacheEverything:false}});
     lastStatus=response.status;
     const body=await response.text();
     if(response.ok&&htmlSizeLooksUseful(body)){
       return json({ok:true,format:"html",source:new URL(candidate).hostname,requested_url:target,html:body});
     }
     lastError=response.ok?"response_not_useful":"HTTP "+response.status;
   }catch(e){lastError=String(e?.message||e);}
 }

 try{
   const jina="https://r.jina.ai/"+target;
   const response=await fetch(jina,{headers:{"User-Agent":"Mozilla/5.0","Accept":"text/plain,text/markdown;q=0.9,*/*;q=0.8"},redirect:"follow"});
   lastStatus=response.status;
   const markdown=await response.text();
   if(response.ok&&markdown.length>=1200&&!/error|forbidden|access denied|cloudflare/i.test(markdown.slice(0,500))){
     const html=markdownToHtml(markdown);
     if(html.length>=500){
       return json({ok:true,format:"markdown-html",source:"r.jina.ai",requested_url:target,html});
     }
   }
   lastError=response.ok?"jina_response_not_useful":"Jina HTTP "+response.status;
 }catch(e){lastError=String(e?.message||e);}

 return json({ok:false,error:"zerozero_unavailable",status:lastStatus,detail:lastError},502);
}};