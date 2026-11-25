import sys, time, re, os
sys.path.insert(0,"/home/user/gitreview")
import git_history as gh
out=sys.argv[2]
t=time.perf_counter()
rep=gh.generate_report(sys.argv[1],"release/SG_1.1.0","origin",out,None,None,None,os.path.join(os.path.dirname(out),"stats.html"),log=lambda s:None)
print("generate %.1fs" % (time.perf_counter()-t))
r=rep.repos[0]; print(r.state, "files",len(r.files),"commits",len(r.commits))
page=rep.html
old=re.sub(r'<pre class="diff" data-z="([^"]*)" data-n="\d+" style="--n:\d+"></pre>', lambda m:'<pre class="diff">'+gh._render_diff(gh.unpack_diff(m.group(1)))+'</pre>', page)
open(os.path.join(os.path.dirname(out),"old_format.html"),"w",encoding="utf-8").write(old)
lines=sum(len(gh.unpack_diff(m)) for m in re.findall(r'data-z="([^"]*)"',page))
print("diff lines",lines)
print("new MB %.1f  old-format MB %.1f" % (len(page.encode())/1e6, len(old.encode())/1e6))
