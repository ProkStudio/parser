// All account/message data is synthetic. Native file pickers are tested separately.
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import assert from 'node:assert/strict';

await mkdir('.qa',{recursive:true});
const child=spawn(process.env.PYTHON || (process.platform==='win32'?'python':'python3'),['-m','tests.fixture_server'],{stdio:['ignore','pipe','inherit']});
const info=await new Promise((resolve,reject)=>{let output='';const timer=setTimeout(()=>reject(new Error('Fixture timeout')),10000);child.stdout.on('data',chunk=>{output+=chunk;const line=output.split('\n')[0];try{const value=JSON.parse(line);clearTimeout(timer);resolve(value);}catch{}});child.on('error',reject);});
const browser=await chromium.launch({headless:true,...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:existsSync('/usr/local/bin/chromium')?{executablePath:'/usr/local/bin/chromium'}:{})});
const checks=[];
try {
  const context=await browser.newContext({viewport:{width:1120,height:780}});
  const page=await context.newPage();const errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.addInitScript(()=>{
    window.__nativeCalls=[];
    window.pywebview={api:{
      window_ready:async()=>({ok:true}),
      import_accounts:async(token,kind,api_id,api_hash,passcode)=>{
        window.__nativeCalls.push({kind,hasPasscode:!!passcode});
        const req=async(path,body)=>{const response=await fetch(path,{method:'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:JSON.stringify(body)});const result=await response.json();if(!response.ok)throw new Error(result.error);return result;};
        const account=await req('/api/accounts',{label:'Учебный импорт '+kind});
        await req('/api/auth/configure',{account_id:account.account_id,api_id:api_id||12345,api_hash:api_hash||'a'.repeat(32)});
        return {items:[{ok:true,name:'Учебный '+kind,id:account.account_id}]};
      },
      export_messages:async()=>({ok:true,name:'synthetic-results.json'})
    }};
  });
  await page.goto(`http://127.0.0.1:${info.port}/#key=${info.key}`);
  // Locator waits use Playwright's isolated world, not page-side eval polling.
  await page.locator('#version').filter({hasText:'0.2.0'}).waitFor();
  assert.equal(await page.locator('nav [data-view]').count(),3);
  async function shot(name){
    await page.waitForTimeout(150);
    const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth+1);
    assert.equal(overflow,false,name+' horizontal overflow');
    const dialogs=await page.locator('dialog[open]').evaluateAll(ds=>ds.map(d=>({id:d.id,width:d.scrollWidth,client:d.clientWidth})));
    assert.ok(dialogs.every(d=>d.width<=d.client+1),name+' dialog overflow');
    assert.ok(!(await page.locator('body').innerText()).includes('\ufffd'),name+' damaged UI text');
    await page.screenshot({path:'.qa/'+name+'.png'});checks.push(name);
  }
  await shot('01-accounts-empty');
  await page.click('#addAccount');await shot('02-session-import');
  await page.fill('#sessionApiId','12345');await page.fill('#sessionApiHash','a'.repeat(32));await page.check('#sessionForm input[type=checkbox]');
  await page.click('#importSession');await page.waitForSelector('#importReport:not([hidden])');
  assert.match(await page.locator('#importReport').innerText(),/добавлен/);
  await page.click('[data-method=tdata]');await shot('03-tdata-import');
  await page.fill('#tdataPasscode','synthetic-local-passcode');await page.check('#tdataForm input[type=checkbox]');await page.click('#importTdata');
  await page.waitForSelector('#importReport:not([hidden])');assert.equal(await page.inputValue('#tdataPasscode'),'');
  assert.deepEqual(await page.evaluate(()=>window.__nativeCalls.map(x=>x.kind)),['session','tdata']);
  await page.click('[data-close=accountDialog]');
  await page.locator('#accountList .account-row').first().getByRole('button',{name:'Проверить',exact:true}).click();
  await page.locator('#accountList').filter({hasText:'Подключён'}).waitFor();
  await shot('04-accounts-populated');
  await page.click('nav [data-view=proxies]');await shot('05-proxies-empty');
  await page.click('#addProxy');await shot('06-proxy-dialog');
  await page.fill('#proxyLabel','Учебный SOCKS5');await page.fill('#proxyHost','127.0.0.1');await page.fill('#proxyPort','1080');await page.fill('#proxyUser','synthetic-user');await page.fill('#proxyPassword','synthetic-password');
  await page.click('#proxyForm [type=submit]');await page.waitForSelector('#proxyDialog:not([open])',{state:'attached'});
  await shot('07-proxies-populated');assert.ok(!(await page.locator('#proxyList').innerText()).includes('synthetic-password'));
  await page.click('nav [data-view=accounts]');await page.locator('#accountList summary').first().click();await shot('08-account-settings');
  await page.locator('#accountList select').first().selectOption({label:'Учебный SOCKS5'});
  await page.locator('#accountList .account-row').first().getByRole('button',{name:'Применить',exact:true}).click();
  await page.locator('#accountList .account-row').first().getByRole('button',{name:'Проверить',exact:true}).click();
  await page.click('#addAccount');await page.click('[data-method=phone]');await page.click('#beginPhone');
  await page.waitForSelector('#authDialog[open]');await shot('09-phone-config');
  await page.fill('#apiId','12345');await page.fill('#apiHash','a'.repeat(32));await page.click('#configForm [type=submit]');await page.waitForSelector('#phoneForm:not([hidden])');
  await page.fill('#phone','+79990000000');await page.click('#phoneForm [type=submit]');await page.waitForSelector('#codeForm:not([hidden])');await shot('10-phone-code');
  await page.fill('#code','22222');await page.click('#codeForm [type=submit]');await page.waitForSelector('#passwordForm:not([hidden])');await shot('11-phone-2fa');
  await page.fill('#password','synthetic');await page.click('#passwordForm [type=submit]');await page.waitForSelector('[data-auth-step=ready]:not([hidden])');await page.click('[data-auth-step=ready] button');
  await page.click('nav [data-view=tasks]');await page.waitForSelector('#allJobs .job-card');await shot('12-tasks');
  await page.click('#newTask');await shot('13-new-task');await page.click('#taskDialog .advanced summary');await shot('14-task-filters');
  await page.fill('#sources','https://example.invalid/not-telegram');await page.click('#createJob');await page.waitForSelector('#taskDialog [data-error]:not([hidden])');
  const errorVisible=await page.locator('#taskDialog [data-error]').evaluate(el=>{const box=el.getBoundingClientRect();const dialog=el.closest('dialog').getBoundingClientRect();return box.top>=dialog.top && box.bottom<=dialog.bottom;});assert.ok(errorVisible,'Task error must be inside the visible dialog');await shot('15-task-error');
  await page.fill('#sources','@synthetic_new_channel');await page.click('#createJob');await page.waitForSelector('#taskDialog:not([open])',{state:'attached'});
  const job=page.locator('#allJobs .job-card').filter({hasText:'@synthetic_new_channel'});await job.getByRole('button',{name:'Пауза',exact:true}).click();await job.filter({hasText:'На паузе'}).waitFor();
  await job.getByRole('button',{name:'Отменить',exact:true}).click();await shot('16-confirmation');await page.click('#confirmOk');
  await page.click('#showResults');await page.waitForSelector('#messageList .message-card');await shot('17-results');
  assert.equal(await page.evaluate(()=>window.parserInjected),undefined);
  await page.fill('#search','нет_такого_синтетического_текста');await page.waitForTimeout(450);await page.waitForSelector('#messageList .empty');await shot('18-results-empty');
  await page.fill('#search','дизайн');await page.waitForTimeout(450);await page.waitForSelector('#messageList .message-card');await page.click('#exportButton');await page.click('[data-close=resultsDialog]');
  await page.click('#helpButton');await shot('19-help');await page.click('[data-close=view-guide]');
  await page.setViewportSize({width:390,height:844});
  for(const view of ['accounts','proxies','tasks']){await page.click('nav [data-view='+view+']');await shot('mobile-'+view);}
  await page.click('nav [data-view=accounts]');await page.click('#addAccount');await shot('mobile-session');await page.click('[data-method=tdata]');await shot('mobile-tdata');await page.click('[data-close=accountDialog]');
  await page.click('nav [data-view=proxies]');await page.click('#addProxy');await shot('mobile-proxy-dialog');await page.click('[data-close=proxyDialog]');
  await page.click('nav [data-view=tasks]');await page.click('#newTask');await shot('mobile-new-task');await page.click('[data-close=taskDialog]');
  await page.click('#showResults');await shot('mobile-results');await page.click('[data-close=resultsDialog]');
  await page.setViewportSize({width:1120,height:780});await page.click('nav [data-view=accounts]');
  await page.locator('#toast').waitFor({state:'hidden'});
  // Self-contained idle DOM snapshot for the mandatory visual capture helper.
  const css=await readFile('parser_app/static/styles.css','utf8');
  const snapshot=await page.evaluate(()=>{const clone=document.documentElement.cloneNode(true);clone.querySelectorAll('script,link').forEach(e=>e.remove());return '<!doctype html>'+clone.outerHTML;});
  await writeFile('.qa/accounts-snapshot.html',snapshot.replace('</head>','<style>'+css+'</style></head>'));
  assert.deepEqual(errors,[],'JavaScript errors');
  await writeFile('.qa/browser-report.json',JSON.stringify({ok:true,screenshots:checks,viewportWidths:[1120,390],nativeFileDialogs:'mocked; separately covered by Python bridge tests',liveTelegram:false},null,2));
  console.log('PASS: three tabs, login/2FA, both import bridges, proxy assignment, task validation/pause/cancel, search/export, XSS, responsive states');
  await context.close();
} finally {await browser.close();child.kill();}
