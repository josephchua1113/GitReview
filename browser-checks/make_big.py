import os, random, subprocess, sys
root=sys.argv[1]; random.seed(7)
remote=os.path.join(root,"remote.git"); work=os.path.join(root,"work"); repos=os.path.join(root,"repos")
def git(*a, cwd=None, env=None): subprocess.run(["git",*a],cwd=cwd,check=True,capture_output=True,env=env)
git("init","--bare","-q",remote); git("init","-q","-b","master",work)
words="user order payment account request response mapper builder service amount currency status limit token session cache value index count total item list map key name id date time retry error result".split()
types="String int long BigDecimal List<String> Map<String,Object> Optional<User> boolean".split()
def line(i):
    r=random.random(); a,b,c=random.sample(words,3)
    if r<0.3: return f"    private {random.choice(types)} {a}{b.title()}{i} = {c}Service.get{b.title()}({random.randint(0,9999)});"
    if r<0.55: return f"        if ({a}.get{b.title()}() != null && {c}Count > {random.randint(1,500)}) {{ log.info(\"{a} {b} {c} %s\", {a}Id); }}"
    if r<0.75: return f"    public {random.choice(types)} {a}{c.title()}({random.choice(types)} {b}) {{ return {b}.{c}(); }}"
    if r<0.9: return f"        // {a} {b} {c} handling for case {random.randint(1,99999)}"
    return f"        {a}{b.title()}.put(\"{c}_{i}\", {random.randint(0,10**6)});"
N=1500
pkgs=["controller","service","mapper","config","util","api","processor","vo"]
files=[f"src/main/java/com/acme/{pkgs[i%8]}/{random.choice(words).title()}{random.choice(words).title()}{['Controller','Service','Mapper','Config','Utils','Api','Processor','VO'][i%8]}{i}.java" for i in range(N)]
for f in files:
    p=os.path.join(work,f); os.makedirs(os.path.dirname(p),exist_ok=True)
    open(p,"w").write("\n".join(line(i) for i in range(150))+"\n")
env=dict(os.environ,GIT_AUTHOR_NAME="Base",GIT_AUTHOR_EMAIL="b@x",GIT_COMMITTER_NAME="Base",GIT_COMMITTER_EMAIL="b@x")
git("add","-A",cwd=work); git("commit","-qm","base",cwd=work,env=env)
git("branch","release/SG_1.0.0",cwd=work)
git("checkout","-qb","release/SG_1.1.0",cwd=work)
authors=["Ann","Bob","Cy","Dee","Eve","Fay","Gus","Hal"]
for c in range(300):
    for f in random.sample(files,25):
        p=os.path.join(work,f); ls=open(p).read().split("\n")
        for _ in range(40):
            k=random.randrange(len(ls)); ls[k]=line(k)
        ls[10:10]=[line(j) for j in range(30)]
        open(p,"w").write("\n".join(ls))
    a=random.choice(authors)
    e=dict(os.environ,GIT_AUTHOR_NAME=a,GIT_AUTHOR_EMAIL=a.lower()+"@x",GIT_COMMITTER_NAME=a,GIT_COMMITTER_EMAIL=a.lower()+"@x")
    git("commit","-qam",f"PAY-{c}: change {random.choice(words)} {random.choice(words)}\n\nDetails for change {c}.",cwd=work,env=e)
git("remote","add","origin",remote,cwd=work); git("push","-q","origin","--all",cwd=work)
os.makedirs(repos); git("clone","-q",remote,os.path.join(repos,"bigsvc"))
print("ok")
