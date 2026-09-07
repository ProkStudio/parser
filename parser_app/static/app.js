"use strict";
const $ = id => document.getElementById(id);
const labels = {queued:"В очереди",running:"Собираем",waiting:"Ждём Telegram",paused:"На паузе",cancelled:"Отменена",failed:"Ошибка",completed:"Завершена"};
const kinds = {session:"Telethon .session",tdata:"Telegram Desktop",login:"Вход по номеру"};
const num = value => new Intl.NumberFormat("ru-RU").format(value || 0);
const date = value => new Intl.DateTimeFormat("ru-RU",{day:"2-digit",month:"short",hour:"2-digit",minute:"2-digit",timeZone:"UTC"}).format(new Date(value));
let token="", currentView="accounts", accounts=[], proxies=[], status=null, authId="", authState=null;
let page=1, jobsOffset=0, refreshing=false, searchTimer, toastTimer, messageRequest=0;
const signatures = new Map(), proxyChecks = new Map();
function node(tag,cls="",text="") { const el=document.createElement(tag); if(cls)el.className=cls; el.textContent=String(text); return el; }
function button(label,cls,action) { const el=node("button",cls,label); el.type="button"; el.onclick=()=>withBusy(el,action); return el; }
function showError(error) {
  const message=error.message || String(error);
  const open=[...document.querySelectorAll("dialog[open]")].reverse().find(d=>d.querySelector("[data-error]"));
  if(open) { const box=open.querySelector("[data-error]"); box.textContent=message; box.hidden=false; box.scrollIntoView({block:"nearest"}); }
  else { $("alertText").textContent=message; $("alert").hidden=false; }
}
function notify(message) { clearTimeout(toastTimer); $("toast").textContent=message; $("toast").hidden=false; toastTimer=setTimeout(()=>{$("toast").hidden=true;},5500); }
function openDialog(id) { const dialog=$(id); const error=dialog.querySelector("[data-error]"); if(error) error.hidden=true; if(!dialog.open)dialog.showModal(); }
async function api(path,{body,raw,...options}={}) {
  let response;
  try { response=await fetch(path,{...options,cache:"no-store",credentials:"omit",headers:{Authorization:"Bearer "+token,...(body!==undefined?{"Content-Type":"application/json"}:{})},...(body!==undefined?{body:JSON.stringify(body)}:{})}); }
  catch { throw new Error("Нет связи с локальным приложением. Перезапустите Parser."); }
  if(!response.ok) {const error=await response.json().catch(()=>({})); throw new Error(error.error || "Не удалось выполнить запрос.");}
  return raw?response:response.json();
}
const post=(path,body={})=>api(path,{method:"POST",body});
async function withBusy(el,action) {
  if(el.disabled)return;
  const label=el.textContent; el.disabled=true; el.textContent="Подождите…";
  const error=el.closest("dialog")?.querySelector("[data-error]"); if(error)error.hidden=true;
  try {await action();}catch(err){showError(err);}finally{el.disabled=false;el.textContent=label;}
}
function confirmAction(title,description,label) {
  $("confirmTitle").textContent=title; $("confirmDescription").textContent=description; $("confirmOk").textContent=label;
  openDialog("confirmDialog"); $("confirmCancel").focus();
  return new Promise(resolve=>{const finish=value=>{$("confirmDialog").close();resolve(value);};$("confirmCancel").onclick=()=>finish(false);$("confirmOk").onclick=()=>finish(true);$("confirmDialog").oncancel=event=>{event.preventDefault();finish(false);};});
}
function empty(target,title,text,symbol="◎") {const box=node("div","empty");box.append(node("span","empty-icon",symbol),node("h2","",title),node("p","",text));target.replaceChildren(box);}
function navigate(view) {
  if(!["accounts","proxies","tasks"].includes(view))return; currentView=view;
  document.querySelectorAll(".view").forEach(el=>{el.hidden=el.id!=="view-"+view;});
  document.querySelectorAll("nav [data-view]").forEach(el=>{const active=el.dataset.view===view;el.classList.toggle("selected",active);if(active)el.setAttribute("aria-current","page");else el.removeAttribute("aria-current");});
  $("main").focus({preventScroll:true}); if(view==="tasks")loadJobs().catch(showError);
}
document.addEventListener("click",event=>{const nav=event.target.closest("nav [data-view]");if(nav)navigate(nav.dataset.view);const close=event.target.closest("[data-close]");if(close)$(close.dataset.close).close();});
function canRender(id,signature) {
  const target=$(id);
  if(target.contains(document.activeElement)||target.querySelector("details[open]"))return false;
  if(signatures.get(id)===signature)return false;
  signatures.set(id,signature);return true;
}
function renderAccounts() {
  const target=$("accountList"); if(!canRender("accountList",JSON.stringify([accounts,proxies])))return;
  if(!accounts.length){empty(target,"Начните с аккаунта","Импортируйте .session, выберите папку tdata или войдите по номеру телефона.");return;}
  target.replaceChildren(...accounts.map(account=>{
    const row=node("article","account-row"),top=node("div","row-top"),identity=node("div","identity"),text=node("div","identity-text");
    identity.append(node("span","avatar",Array.from(account.label).slice(0,1).join("").toUpperCase()));
    text.append(node("strong","",account.label));
    const proxy=proxies.find(p=>p.id===account.proxy_id);
    text.append(node("p","meta",kinds[account.kind]+" · "+(proxy?proxy.label:"Без прокси")));
    text.append(node("span","badge "+(account.authorized?"ready":account.error?"failed":""),account.authorized?"● Подключён":account.error?"● Требует внимания":"○ Не проверен"));
    identity.append(text);const actions=node("div","actions");
    actions.append(button("Проверить","",async()=>{await post("/api/accounts/"+account.id+"/check");document.activeElement?.blur();signatures.delete("accountList");await refresh();notify("Подключение аккаунта проверено");}));
    top.append(identity,actions);row.append(top);
    if(account.error)row.append(node("p","job-error",account.error));
    const details=node("details","account-settings"),panel=node("div","settings-panel"),assignment=node("div","actions"),label=node("label","","Прокси для аккаунта"),select=node("select");
    select.setAttribute("aria-label","Прокси: "+account.label);const direct=node("option","","Без прокси");direct.value="";select.append(direct);
    for(const p of proxies){const option=node("option","",p.label);option.value=p.id;select.append(option);}select.value=account.proxy_id;
    label.append(select);assignment.append(label,button("Применить","",async()=>{await post("/api/accounts/"+account.id+"/proxy",{proxy_id:select.value});details.open=false;document.activeElement?.blur();signatures.delete("accountList");await refresh();notify("Прокси назначен. Нажмите «Проверить» у аккаунта.");}));
    const footer=node("div","row-footer actions");footer.append(button(account.authorized?"Аккаунт подключён":"Войти / настроить","quiet",async()=>{authId=account.id;authState=await post("/api/accounts/"+account.id+"/select");renderAuth(authState);openDialog("authDialog");}));
    footer.append(button("Удалить из Parser","quiet danger-text",async()=>{if(!await confirmAction("Удалить аккаунт из Parser?","Удалится только локальная копия сессии. Исходные файлы и сессия в Telegram останутся. Собранные сообщения сохранятся; задачи этого аккаунта нельзя будет продолжить.","Удалить локально"))return;await post("/api/accounts/"+account.id+"/remove",{confirm:"remove-local-account"});details.open=false;document.activeElement?.blur();signatures.delete("accountList");await refresh();}));
    panel.append(assignment,footer);details.append(node("summary","","Настройки"),panel);row.append(details);return row;
  }));
}
function renderProxies() {
  const target=$("proxyList");if(!canRender("proxyList",JSON.stringify([proxies,[...proxyChecks]])))return;
  if(!proxies.length){empty(target,"Прокси пока нет","Можно работать без прокси. Если он нужен, добавьте SOCKS5 или HTTP CONNECT и назначьте его аккаунту.","⇄");return;}
  target.replaceChildren(...proxies.map(proxy=>{const row=node("article","proxy-row"),top=node("div","row-top"),text=node("div","identity-text"),actions=node("div","actions");text.append(node("strong","",proxy.label),node("p","meta",proxy.type.toUpperCase()+" · "+proxy.host+":"+proxy.port+(proxy.has_auth?" · С авторизацией":"")));
    actions.append(button("Проверить","",async()=>{const result=await post("/api/proxies/"+proxy.id+"/check");proxyChecks.set(proxy.id,"Туннель доступен · "+num(result.latency_ms)+" мс. Авторизация аккаунта не проверялась.");document.activeElement?.blur();renderProxies();}),button("Удалить","quiet danger-text",async()=>{if(!await confirmAction("Удалить прокси?","Сначала отключите этот прокси у аккаунтов, которым он назначен.","Удалить"))return;await post("/api/proxies/"+proxy.id+"/remove",{confirm:"remove-proxy"});document.activeElement?.blur();await refresh();}));top.append(text,actions);row.append(top);if(proxyChecks.has(proxy.id))row.append(node("p","meta",proxyChecks.get(proxy.id)));return row;}));
}
function renderJobs(items) {
  const target=$("allJobs");if(!canRender("allJobs",JSON.stringify(items)))return;
  if(!items.length){empty(target,"Задач пока нет","Создайте задачу, выберите аккаунт и добавьте каналы или группы. Прогресс появится здесь.","≡");return;}
  target.replaceChildren(...items.map(job=>{const row=node("article","job-card"),top=node("div","job-top");top.append(node("strong","job-name",job.source_key),node("span","badge "+job.state,labels[job.state]||job.state));row.append(top,node("p","meta",(job.options.account_label||"Аккаунт из прежней версии")+" · Просмотрено "+num(job.scanned)+" / "+num(job.options.limit)+" · Сохранено "+num(job.matched)));
    const progress=node("progress");progress.max=job.options.limit;progress.value=job.scanned;progress.setAttribute("aria-label","Просмотрено сообщений в пределах лимита");row.append(progress);
    if(job.error)row.append(node("p","job-error",job.error));if(job.state==="waiting"&&job.wait_until)row.append(node("p","job-error","Telegram просит подождать до "+date(job.wait_until)+" UTC. Продолжим автоматически."));
    if(job.completion==="limit")row.append(node("p","help","Лимит достигнут. Создайте следующий сбор с сохранённой позиции."));if(job.stop_request)row.append(node("p","help","Сохраняем позицию и останавливаем запрос…"));
    const actions=node("div","job-actions"),choices=[];if(["queued","running","waiting"].includes(job.state))choices.push(["pause","Пауза"]);if(["paused","failed"].includes(job.state))choices.push(["resume","Продолжить"]);if(["queued","running","waiting","paused","failed"].includes(job.state))choices.push(["cancel","Отменить"]);
    for(const [action,label] of choices){const control=button(label,action==="cancel"?"quiet danger-text":"",async()=>{if(action==="cancel"&&!await confirmAction("Отменить задачу?","Сохранённые сообщения останутся в результатах.","Отменить задачу"))return;await post("/api/jobs/"+job.id+"/"+action);document.activeElement?.blur();await loadJobs();});control.disabled=!!job.stop_request;actions.append(control);}row.append(actions);return row;}));
}
async function loadJobs() {const data=await api("/api/jobs?offset="+jobsOffset);renderJobs(data.items);$("prevJobs").disabled=jobsOffset===0;$("nextJobs").disabled=data.items.length<50;$("jobsPageLabel").textContent="Страница "+(jobsOffset/50+1);}
function updateAccountSelect() {const select=$("jobAccount"),value=select.value;const available=accounts.filter(a=>a.authorized);select.replaceChildren();for(const account of available){const option=node("option","",account.label);option.value=account.id;select.append(option);}if(available.some(a=>a.id===value))select.value=value;if(!available.length){const option=node("option","","Сначала проверьте аккаунт во вкладке «Аккаунты»");option.value="";select.append(option);}$("createJob").disabled=!available.length;}
async function refresh() {
  if(refreshing||!token)return;refreshing=true;
  try {
    const [next,a,p,s]=await Promise.all([api("/api/status"),api("/api/accounts"),api("/api/proxies"),api("/api/sources")]);status=next;accounts=a.items;proxies=p.items;
    $("version").textContent="Parser "+next.version;$("dataDirectory").textContent=next.data_dir;
    $("cooldownNotice").hidden=next.cooldown_until*1000<=Date.now();if(!$("cooldownNotice").hidden)$("cooldownNotice").textContent="Ограничение Telegram: ждём до "+date(next.cooldown_until*1000)+" UTC. Ожидание сохраняется при перезапуске.";
    renderAccounts();renderProxies();if(!$('taskDialog').open)updateAccountSelect();
    const filter=$("sourceFilter"),value=filter.value;filter.replaceChildren();const all=node("option","","Все источники");all.value="";filter.append(all);for(const item of s.items){const option=node("option","",item.title);option.value=String(item.id);filter.append(option);}if(s.items.some(item=>String(item.id)===value))filter.value=value;
    if(currentView==="tasks")await loadJobs();
  }catch(error){showError(error);}finally{refreshing=false;}
}
function renderAuth(state,override) {authState=state;const step=override||(state.authorized?"ready":state.step);document.querySelectorAll("[data-auth-step]").forEach(el=>{el.hidden=el.dataset.authStep!==step;});$("accountName").textContent=state.account||"Аккаунт Telegram";$("authError").hidden=!state.error;$("authError").textContent=state.error||"";}
function chooseMethod(method) {document.querySelectorAll("[data-import]").forEach(el=>{el.hidden=el.dataset.import!==method;});document.querySelectorAll("[data-method]").forEach(el=>el.classList.toggle("selected",el.dataset.method===method));$("importReport").hidden=true;$("accountDialog").querySelector("[data-error]").hidden=true;}
for(const el of document.querySelectorAll("[data-method]"))el.onclick=()=>chooseMethod(el.dataset.method);
function nativeReady() {return typeof window.pywebview?.api?.import_accounts==="function";}
function updateNative() {$("nativeHint").hidden=nativeReady();}
$("addAccount").onclick=()=>{chooseMethod("session");updateNative();openDialog("accountDialog");};
$("beginPhone").onclick=()=>withBusy($("beginPhone"),async()=>{const next=await post("/api/accounts",{});authId=next.account_id;$("accountDialog").close();renderAuth(next);openDialog("authDialog");await refresh();});
for(const kind of ["session","tdata"]) {
  $(kind+"Form").onsubmit=async event=>{event.preventDefault();const el=$(kind==="session"?"importSession":"importTdata");await withBusy(el,async()=>{
    if(!nativeReady())throw new Error("Откройте Parser как отдельное приложение. В браузере выбор локальных сессий отключён.");
    let result;try {result=await window.pywebview.api.import_accounts(token,kind,$("sessionApiId").value,$("sessionApiHash").value,$("tdataPasscode").value);}finally{$("tdataPasscode").value="";}
    if(result.error)throw new Error(result.error);if(result.cancelled)return;
    const report=$("importReport");report.replaceChildren();for(const item of result.items)report.append(node("p",item.ok?"success-note":"warning",(item.ok?"✓ ":"Не импортирован: ")+item.name+(item.ok?" — добавлен, нажмите «Проверить» в списке аккаунтов.":" — "+item.error)));
    report.hidden=false;if(result.items.some(item=>item.ok))$("sessionApiHash").value="";await refresh();report.scrollIntoView({block:"nearest"});
  });};
}
for(const [form,action,fields] of [["configForm","configure",{api_id:"apiId",api_hash:"apiHash"}],["phoneForm","send_code",{phone:"phone"}],["codeForm","verify_code",{code:"code"}],["passwordForm","verify_password",{password:"password"}]]) {
  $(form).onsubmit=async event=>{event.preventDefault();await withBusy($(form).querySelector('[type="submit"]'),async()=>{const body={account_id:authId,...Object.fromEntries(Object.entries(fields).map(([k,id])=>[k,$(id).value]))};let next;try{next=await post("/api/auth/"+action,body);}finally{for(const key of ["code","password"])if(fields[key])$(fields[key]).value="";}if(fields.api_hash)$(fields.api_hash).value="";renderAuth(next);await refresh();const input=document.querySelector('[data-auth-step="'+next.step+'"] input');if(input)input.focus();});};
}
$("editConfig").onclick=()=>renderAuth(authState,"settings");$("backToPhone").onclick=()=>renderAuth(authState,"phone");
$("addProxy").onclick=()=>openDialog("proxyDialog");
$("proxyForm").onsubmit=event=>{event.preventDefault();withBusy($("proxyForm").querySelector('[type="submit"]'),async()=>{await post("/api/proxies",{label:$("proxyLabel").value,type:$("proxyType").value,host:$("proxyHost").value,port:$("proxyPort").value,username:$("proxyUser").value,password:$("proxyPassword").value});$("proxyForm").reset();$("proxyDialog").close();await refresh();notify("Прокси добавлен. Назначьте его в настройках аккаунта.");});};
$("newTask").onclick=()=>{updateAccountSelect();openDialog("taskDialog");};
$("jobForm").onsubmit=event=>{event.preventDefault();withBusy($("createJob"),async()=>{await post("/api/jobs",{account_id:$("jobAccount").value,sources:$("sources").value,limit:$("limit").value,include:$("include").value,exclude:$("exclude").value,date_from:$("dateFrom").value,date_to:$("dateTo").value,media:$("media").value,incremental:$("incremental").checked});$("taskDialog").close();jobsOffset=0;await loadJobs();notify("Задача добавлена в очередь");});};
function messageQuery() {return new URLSearchParams({q:$("search").value.trim(),source:$("sourceFilter").value});}
async function loadMessages() {const serial=++messageRequest;const query=messageQuery();query.set("page",String(page));const data=await api("/api/messages?"+query);if(serial!==messageRequest)return;
  $("resultCount").textContent="Найдено: "+num(data.total);const pages=Math.max(1,Math.ceil(data.total/data.page_size));$("pageLabel").textContent=page+" / "+pages;$("prevPage").disabled=page<=1;$("nextPage").disabled=page>=pages;$("exportButton").disabled=!data.total;
  if(!data.items.length){empty($("messageList"),"Сообщений не найдено","Запустите сбор или измените поисковый запрос.","≡");return;}
  $("messageList").replaceChildren(...data.items.map(message=>{const card=node("article","message-card"),meta=node("div","message-meta");meta.append(node("strong","",message.source_title),node("span","",date(message.sent_at)+" UTC"));card.append(meta,node("p","message-text",message.text?Array.from(message.text).slice(0,500).join("")+(message.text.length>500?"…":""):"Медиасообщение без подписи"));if(message.text.length>500){const details=node("details");details.append(node("summary","","Полный текст"),node("p","",message.text));card.append(details);}const bottom=node("div","message-bottom");bottom.append(node("span","","ID "+message.message_id+" · "+message.media));try{const url=new URL(message.link);if(url.protocol==="https:"&&url.host==="t.me"&&!url.username&&!url.password){const link=node("a","","В Telegram ↗");link.href=url.href;link.target="_blank";link.rel="noreferrer";bottom.append(link);}}catch{}card.append(bottom);return card;}));
}
$("showResults").onclick=()=>{openDialog("resultsDialog");loadMessages().catch(showError);};
$("search").oninput=()=>{clearTimeout(searchTimer);page=1;searchTimer=setTimeout(()=>loadMessages().catch(showError),300);};
$("sourceFilter").onchange=()=>{page=1;loadMessages().catch(showError);};
$("prevPage").onclick=()=>{page=Math.max(1,page-1);loadMessages().catch(showError);};$("nextPage").onclick=()=>{page++;loadMessages().catch(showError);};
$("prevJobs").onclick=()=>{jobsOffset=Math.max(0,jobsOffset-50);loadJobs().catch(showError);};$("nextJobs").onclick=()=>{jobsOffset+=50;loadJobs().catch(showError);};
$("exportButton").onclick=()=>withBusy($("exportButton"),async()=>{const fmt=$("exportFormat").value;if(nativeReady()){const result=await window.pywebview.api.export_messages(token,fmt,$("search").value.trim(),$("sourceFilter").value);if(result.error)throw new Error(result.error);if(result.cancelled)return;notify("Сохранено: "+result.name);}else{const query=messageQuery();query.set("format",fmt);const response=await api("/api/export?"+query,{raw:true});const url=URL.createObjectURL(await response.blob());const link=node("a");link.href=url;link.download="parser-messages."+fmt;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);notify("Выборка экспортирована");}});
$("helpButton").onclick=()=>openDialog("view-guide");$("dismissAlert").onclick=()=>{$("alert").hidden=true;};
try {const key=new URLSearchParams(location.hash.slice(1)).get("key");if(key){history.replaceState(null,"",location.pathname+location.search);sessionStorage.setItem("parser-key",key);}token=sessionStorage.getItem("parser-key")||"";}catch{showError(new Error("Не удалось открыть локальное хранилище окна. Перезапустите Parser."));}
function bridgeReady(){updateNative();if(window.pywebview?.api?.window_ready)window.pywebview.api.window_ready(token,[...document.querySelectorAll("nav [data-view] span:last-child")].map(el=>el.textContent)).catch(showError);}
window.addEventListener("pywebviewready",bridgeReady);if(nativeReady())bridgeReady();
if(token){refresh();setInterval(()=>{if(!document.hidden)refresh();},2500);}else{showError(new Error("Запустите Parser из меню «Пуск» или через python -m parser_app. Ключ окна отсутствует."));}
