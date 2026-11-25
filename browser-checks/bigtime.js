const {chromium}=require('playwright');
(async()=>{const b=await chromium.launch();
for(const f of process.argv.slice(2)){
  const runs=[];
  for(let k=0;k<3;k++){
    const p=await b.newPage({viewport:{width:1400,height:900}}); const errs=[]; p.on('pageerror',e=>errs.push(e.message));
    const t0=Date.now(); await p.goto('file://'+f,{waitUntil:'load',timeout:600000});
    // wait for a frame after load so the first screen is painted
    await p.evaluate(()=>new Promise(r=>requestAnimationFrame(()=>setTimeout(r,0))));
    const t=Date.now()-t0;
    const nav=await p.evaluate(()=>{const n=performance.getEntriesByType('navigation')[0];return Math.round(n.loadEventEnd);});
    const mem=await p.evaluate(()=>performance.memory?Math.round(performance.memory.usedJSHeapSize/1e6):null);
    const nodes=await p.evaluate(()=>document.getElementsByTagName('*').length);
    runs.push({wall:t,load:nav,heapMB:mem,nodes,errs:errs.length});
    if(k===2 && f.endsWith('index.html')){
      let s=Date.now(); await p.fill('#qd','paymentcount'); await p.press('#qd','Enter');
      await p.waitForFunction(()=>/^Match 1 of|No code/.test(document.getElementById('qd-state').textContent),null,{timeout:120000});
      const st=await p.textContent('#qd-state'); console.log('  search all diffs: %d ms — %s',Date.now()-s,st);
      s=Date.now(); await p.press('#qd','Enter'); await p.waitForFunction(()=>/^Match 2 of/.test(document.getElementById('qd-state').textContent));
      console.log('  next match (cached): %d ms',Date.now()-s);
      s=Date.now(); await p.click('#draw-all'); await p.waitForFunction(()=>document.getElementById('draw-all').textContent==='All diffs drawn',null,{timeout:600000});
      console.log('  draw all: %d ms, heap %s MB, errors %d',Date.now()-s, await p.evaluate(()=>performance.memory?Math.round(performance.memory.usedJSHeapSize/1e6):null), errs.length);
    }
    await p.close();
  }
  console.log(f.split('/').pop(), JSON.stringify(runs));
}
await b.close();})();
