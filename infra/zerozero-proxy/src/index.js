const ALLOWED_HOSTS=new Set(["www.zerozero.pt","zerozero.pt","www.zerozero.africa","zerozero.africa","www.zerozero.dk","zerozero.dk","www.zerozero.football","zerozero.football","www.zerozero.gr","zerozero.gr"]);
const MIRROR_HOSTS=["www.zerozero.pt","www.zerozero.africa","www.zerozero.dk","www.zerozero.football","www.zerozero.gr"];
const corsHeaders=()=>({"Access-Control-Allow-Origin":"*","Access-Control-Allow-Methods":"GET, OPTIONS","Access-Control-Allow-Headers":"Content-Type","Cache-Control":"no-store"});
const validArticlePath=p=>p.startsWith("/noticias/")&&p.length>12;
export default{async fetch(request){
 const headers=corsHeaders();
 if(request.method==="OPTIONS")return new Response(null,{headers});
 if(request.method!=="GET")return new Response(JSON.stringify({error:"method_not_allowed"}),{status:405,headers:{...headers,"Content-Type":"application/json"}});
 const target=new URL(request.url).searchParams.get("url");
 if(!target)return new Response(JSON.stringify({error:"missing_url"}),{status:400,headers:{...headers,"Content-Type":"application/json"}});
 let parsed;try{parsed=new URL(target)}catch{return new Response(JSON.stringify({error:"invalid_url"}),{status:400,headers:{...headers,"Content-Type":"application/json"}});}
 if(!ALLOWED_HOSTS.has(parsed.hostname)||!validArticlePath(parsed.pathname))return new Response(JSON.stringify({error:"url_not_allowed"}),{status:403,headers:{...headers,"Content-Type":"application/json"}});
 const candidates=MIRROR_HOSTS.map(host=>new URL(parsed.pathname+parsed.search,"https://"+host).toString());
 const requestHeaders={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140 Safari/537.36","Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8","Accept-Language":"pt-PT,pt;q=0.9,en;q=0.7","Cache-Control":"no-cache","Pragma":"no-cache"};
 let lastStatus=502,lastError="";
 for(const candidate of candidates){try{
  const response=await fetch(candidate,{headers:requestHeaders,redirect:"follow",cf:{cacheTtl:0,cacheEverything:false}});
  lastStatus=response.status;if(!response.ok){lastError="HTTP "+response.status;continue;}
  const html=await response.text();if(html.length<5000){lastError="response_too_small";continue;}
  return new Response(JSON.stringify({ok:true,source:new URL(candidate).hostname,requested_url:target,html}),{status:200,headers:{...headers,"Content-Type":"application/json;charset=UTF-8"}});
 }catch(e){lastError=String(e?.message||e);}}
 return new Response(JSON.stringify({ok:false,error:"zerozero_unavailable",status:lastStatus,detail:lastError}),{status:502,headers:{...headers,"Content-Type":"application/json;charset=UTF-8"}});
}};
