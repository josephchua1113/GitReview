const {chromium}=require('playwright');
const fs=require('fs');
const dir=process.argv[2];
const expected=JSON.parse(fs.readFileSync(dir+'/expected.json'));
let fails=0; function check(name,ok,extra){console.log((ok?'PASS ':'FAIL ')+name+(extra?' — '+extra:'')); if(!ok) fails++;}
(async()=>{
  const b=await chromium.launch();
  const p=await b.newPage({viewport:{width:1400,height:900}});
  const errs=[]; p.on('pageerror',e=>errs.push(e.message)); p.on('console',m=>{if(m.type()==='error')errs.push(m.text());});
  await p.goto('file://'+dir+'/index.html'); await p.waitForTimeout(800);
  const st=await p.evaluate(()=>{const a=[...document.querySelectorAll('pre.diff[data-z]')];return {n:a.length,drawn:a.filter(x=>x.classList.contains('drawn')).length};});
  check('lazy: some but not all diffs drawn at load', st.drawn>0 && st.drawn<st.n, JSON.stringify(st));
  check('DecompressionStream available from file://', await p.evaluate(()=>typeof DecompressionStream!=='undefined'));
  // existing path/message filter still works
  await p.fill('#q','Controller'); await p.waitForTimeout(200);
  const vis=await p.evaluate(()=>[...document.querySelectorAll('.file')].filter(f=>!f.classList.contains('filtered')).map(f=>f.dataset.path));
  check('path search filter', vis.length>0 && vis.every(v=>v.includes('Controller')), vis.length+' files');
  await p.click('#clear-filters');
  // diff search
  await p.fill('#qd','paymentcount'); await p.press('#qd','Enter'); await p.waitForTimeout(1500);
  const s1=await p.evaluate(()=>({state:document.getElementById('qd-state').textContent,cur:document.querySelectorAll('span.hit-cur').length,curText:(document.querySelector('span.hit-cur')||{}).textContent||''}));
  check('diff search finds and jumps', s1.cur===1 && /paymentcount/i.test(s1.curText) && /^Match 1 of \d+/.test(s1.state), JSON.stringify(s1));
  const total=+s1.state.match(/of (\d+)/)[1];
  // expected count from python data
  const want=expected.reduce((a,e)=>a+e.filter(([k,l])=>l.toLowerCase().includes('paymentcount')).length,0);
  check('diff search count matches data', total===want, total+' vs '+want);
  const inView=await p.evaluate(()=>{const r=document.querySelector('span.hit-cur').getBoundingClientRect();return r.top>0&&r.bottom<innerHeight;});
  check('match scrolled into view', inView);
  await p.press('#qd','Enter'); await p.waitForTimeout(500);
  check('next match', (await p.textContent('#qd-state')).startsWith('Match 2 of'));
  await p.press('#qd','Shift+Enter'); await p.waitForTimeout(500);
  check('previous match', (await p.textContent('#qd-state')).startsWith('Match 1 of'));
  // jump to last match (far down) wraps
  await p.press('#qd','Shift+Enter'); await p.waitForTimeout(800);
  const last=await p.evaluate(()=>({s:document.getElementById('qd-state').textContent,inView:(()=>{const e=document.querySelector('span.hit-cur');if(!e)return false;const r=e.getBoundingClientRect();return r.top>0&&r.bottom<innerHeight;})()}));
  check('wrap to last match, drawn and in view', last.s.startsWith('Match '+total+' of') && last.inView, JSON.stringify(last));
  await p.fill('#qd','zz_no_such_text_zz'); await p.press('#qd','Enter'); await p.waitForTimeout(800);
  check('no-match message', (await p.textContent('#qd-state'))==='No code diff line matches.');
  await p.fill('#qd',''); await p.waitForTimeout(500);
  check('clearing search removes highlights', await p.evaluate(()=>document.querySelectorAll('span.hit,span.hit-cur').length===0));
  // draw all
  await p.click('#draw-all'); await p.waitForFunction(()=>document.getElementById('draw-all').textContent==='All diffs drawn',null,{timeout:20000});
  const drawn=await p.evaluate(()=>[...document.querySelectorAll('pre.diff[data-z]')].map(x=>[x.classList.contains('drawn'),[...x.children].map(s=>[s.className.split(' ')[0],s.textContent])]));
  check('draw all: every diff drawn', drawn.every(d=>d[0]));
  let same=drawn.length===expected.length;
  for(let i=0;same&&i<drawn.length;i++){const e=expected[i].map(([k,l])=>['d'+k,l]); if(JSON.stringify(e)!==JSON.stringify(drawn[i][1])){same=false;console.log('  mismatch in pre',i);}}
  check('every drawn diff equals the packed data exactly (no truncation)', same, drawn.reduce((a,d)=>a+d[1].length,0)+' lines');
  // toggles
  await p.click('#toggle-diff');
  check('hide diff column', await p.evaluate(()=>getComputedStyle(document.querySelector('.diff-col')).display==='none'));
  await p.click('#toggle-diff');
  check('show diff column', await p.evaluate(()=>getComputedStyle(document.querySelector('.diff-col')).display!=='none'));
  await p.click('#toggle-details');
  check('hide changes column', await p.evaluate(()=>getComputedStyle(document.querySelector('.detail-col')).display==='none'));
  await p.click('#toggle-details');
  await p.click('#collapse-all');
  check('collapse all', await p.evaluate(()=>[...document.querySelectorAll('details.diff-col')].every(d=>!d.open)));
  await p.click('#expand-all');
  check('expand all', await p.evaluate(()=>[...document.querySelectorAll('details.diff-col')].every(d=>d.open)));
  check('no script errors (main page)', errs.length===0, errs.join(' | '));
  // lazy draw after collapse/expand on a fresh page, and on scroll
  const p2=await b.newPage({viewport:{width:1400,height:900}}); const e2=[]; p2.on('pageerror',e=>e2.push(e.message));
  await p2.goto('file://'+dir+'/index.html'); await p2.waitForTimeout(500);
  await p2.evaluate(()=>window.scrollTo(0,document.body.scrollHeight)); await p2.waitForTimeout(800);
  const lastDrawn=await p2.evaluate(()=>{const a=[...document.querySelectorAll('pre.diff[data-z]')];return a[a.length-1].classList.contains('drawn');});
  check('scrolling to the bottom draws the diffs there', lastDrawn);
  // stats page links
  const sp=await b.newPage({viewport:{width:1400,height:900}}); const e3=[]; sp.on('pageerror',e=>e3.push(e.message));
  await sp.goto('file://'+dir+'/stats.html');
  await sp.click('#tab-btn-details');
  const links=await sp.$$eval('#tab-details td.path a',as=>as.map(a=>a.getAttribute('href')));
  const target=links.filter(h=>h.includes('file/bigsvc/'))[Math.floor(links.filter(h=>h.includes('file/bigsvc/')).length*0.7)];
  await Promise.all([sp.waitForNavigation(), sp.click(`#tab-details td.path a[href="${target}"]`)]);
  await sp.waitForTimeout(2500);
  const land=await sp.evaluate(()=>{const id=decodeURIComponent(location.hash.slice(1));const el=document.getElementById(id);const r=el.getBoundingClientRect();const pre=el.querySelector('pre.diff[data-z]');return {id,top:Math.round(r.top),inView:r.top>=0&&r.top<innerHeight,drawn:pre?pre.classList.contains('drawn'):'no diff'};});
  check('stats link lands on the file row with its diff drawn', land.inView && land.drawn!==false, JSON.stringify(land));
  await sp.waitForTimeout(3000);
  const land2=await sp.evaluate(()=>{const el=document.getElementById(decodeURIComponent(location.hash.slice(1)));return Math.round(el.getBoundingClientRect().top);});
  check('row stays put while nearby diffs are drawn', Math.abs(land2-land.top)<=40, land.top+' -> '+land2);
  check('no script errors (stats + link)', e2.length===0&&e3.length===0, e2.concat(e3).join(' | '));
  await p.screenshot({path:dir+'/../check-main.png'});
  await b.close(); console.log(fails?fails+' FAILED':'ALL PASSED'); process.exit(fails?1:0);
})();
