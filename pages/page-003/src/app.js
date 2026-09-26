const doctors=[{"name":"林悦","photo":13,"meta":"从业12年 · 好评率99% · 上海","intro":"陪你梳理情绪，重建内在力量","tags":["情绪管理","恋爱情感"],"price":300},{"name":"陈安","photo":15,"meta":"从业8年 · 暂无评价 · 杭州","intro":"一起探索关系，找到适合自己的节奏","tags":["人际关系","职场心理"],"price":260}];
const panel=document.querySelector('#panel'),body=document.querySelector('#panel-body');
function openPanel(title,html){document.querySelector('#panel-title').textContent=title;body.innerHTML=html;if(!panel.open)panel.showModal();}
document.querySelector('#close').onclick=()=>panel.close();
panel.addEventListener('click',e=>{if(e.target===panel)panel.close()});
function listing(title,filter=''){const matching=doctors.map((d,i)=>({d,i})).filter(({d})=>!filter||[d.name,d.meta,d.intro,...d.tags].join(' ').includes(filter));openPanel(title,matching.length?matching.map(({d,i})=>'<button class="choice" data-detail="'+i+'">'+d.name+' · '+d.tags.join(' / ')+'<br>¥'+d.price+' 起 / 50分钟</button>').join(''):'<p class="empty">暂未找到匹配的心理师，请尝试其他关键词。</p><button class="choice" data-all>查看全部心理师</button>');}
document.querySelector('#search').onsubmit=e=>{e.preventDefault();listing('搜索结果',e.currentTarget.elements.query.value.trim())};
document.querySelector('#all').onclick=()=>listing('全部心理师');
function detail(i){const d=doctors[i];openPanel(d.name+' · 心理师详情','<p>资深 · 资质认证</p><p>'+d.meta+'</p><p>'+d.intro+'</p><p>擅长：'+d.tags.join('、')+'</p><p>¥'+d.price+' 起 / 50分钟</p><button class="primary" data-book="'+i+'">预约咨询</button>');}
function booking(i){const d=doctors[i];openPanel('预约 '+d.name,'<p>'+d.tags.join(' · ')+' · ¥'+d.price+' 起 / 50分钟</p><p class="notice">此页面为预约演示，未连接服务端；选择不会创建真实预约或产生费用。</p><form id="booking"><label>期望日期<input type="date" id="date" required></label><label>期望时段<select><option>09:00–09:50</option><option>14:00–14:50</option><option>19:00–19:50</option></select></label><button class="primary">预览预约信息</button></form>');document.querySelector('#date').min=new Date().toLocaleDateString('en-CA');document.querySelector('#booking').onsubmit=e=>{e.preventDefault();openPanel('预约信息预览','<p>心理师：'+d.name+'</p><p>期望日期：'+document.querySelector('#date').value+'</p><p>时段：'+e.currentTarget.querySelector('select').value+'</p><p>咨询费用：¥'+d.price+' / 50分钟</p><p class="notice">尚未提交预约。真实预约需接入排期与预约服务。</p>')};}
const themes=['情绪状态','压力情况','人际关系'];
let theme='',step=0,answers=[];
const questions={'情绪状态':['最近一周，你感到情绪低落的频率如何？','最近一周，你难以对日常活动提起兴趣的频率如何？','最近一周，你感到难以平静的频率如何？'],'压力情况':['最近一周，你感到事情超出自己掌控的频率如何？','最近一周，你因任务或责任难以放松的频率如何？','最近一周，压力影响休息的频率如何？'],'人际关系':['最近一周，你在人际互动中感到紧张的频率如何？','最近一周，你觉得自己的需求难以表达的频率如何？','最近一周，你感到缺少理解或支持的频率如何？']};
function themesPage(){openPanel('选择测评主题','<p>了解此刻的心理状态</p>'+themes.map(t=>'<button class="choice" data-theme="'+t+'">'+t+' <span class="chevron"></span></button>').join('')+'<p class="notice">这是简短的自我观察练习，并非经验证的临床量表，不作为医学诊断。</p>');}
document.querySelector('#start').onclick=themesPage;
function explain(t){theme=t;openPanel(t+' · 测评说明','<p>请根据最近一周的真实感受，完成 3 个问题。你可以随时关闭退出。</p><p class="notice">结果仅帮助自我了解，不提供疾病判断或临床评分。报告仅保存在当前浏览器，你可以删除记录。</p><button class="primary" data-begin>开始问答</button>');}
function question(){openPanel(theme+' · '+(step+1)+' / 3','<p>'+questions[theme][step]+'</p>'+['从未','偶尔','经常','几乎每天'].map((s,i)=>'<button class="choice" data-answer="'+i+'">'+s+'</button>').join('')+(step?'<button data-back>上一题</button>':''));}
function getRecords(){try{return JSON.parse(localStorage.getItem('wellness-reports')||'[]')}catch{return []}}
function report(r){const frequent=r.answers.filter(v=>v>=2).length;openPanel(r.theme+' · 自我观察报告','<p class="notice">'+r.date+'</p><h3>结果解读</h3><p>'+ (frequent?'你在 '+frequent+' 项回答中选择了“经常”或“几乎每天”。这些体验值得温柔关注，但不能据此判断任何疾病。':'本次回答以“从未”或“偶尔”为主。感受会随环境变化，仍可以持续观察自己的需要。')+'</p><h3>日常调适建议</h3><p>试着记录感受与触发情境，保持规律休息，为自己安排短暂放松，并向信任的人表达需要。如困扰持续或影响日常生活，可寻求专业支持。</p><p class="notice">测评结果用于自我了解，不作为医学诊断。</p><h3>相关心理师</h3>'+doctors.map((d,i)=>'<button class="choice" data-detail="'+i+'">'+d.name+' · '+d.tags.join(' / ')+'</button>').join(''));}
function records(){const rs=getRecords();openPanel('测评记录',rs.length?rs.map((r,i)=>'<button class="choice" data-report="'+i+'">'+r.theme+'<br>'+r.date+'</button>').join('')+'<button data-clear>删除本机全部记录</button>':'<p class="empty">暂无测评记录</p><button class="primary" data-start>开始测评</button>');}
document.querySelector('#records').onclick=records;
document.addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;const d=b.dataset;
if(d.category){if(d.category==='全部分类')openPanel('全部分类',["心理健康","恋爱情感","人际关系","情绪管理","婚姻家庭","职场心理","个人成长","亲子教育","学业心理"].map(t=>'<button class="choice" data-category="'+t+'">'+t+'</button>').join(''));else listing(d.category,d.category);}
if('detail'in d)detail(Number(d.detail));
if('book'in d)booking(Number(d.book));
if('all'in d)listing('全部心理师');
if('theme'in d)explain(d.theme);
if('begin'in d){step=0;answers=[];question();}
if('answer'in d){answers[step]=Number(d.answer);if(++step<3)question();else{const r={theme,answers:[...answers],date:new Date().toLocaleString('zh-CN')};try{localStorage.setItem('wellness-reports',JSON.stringify([r,...getRecords()].slice(0,50)))}catch{}report(r);}}
if('back'in d){step--;question();}
if('report'in d)report(getRecords()[Number(d.report)]);
if('start'in d)themesPage();
if('clear'in d)openPanel('删除测评记录','<p>将删除当前浏览器中保存的所有测评记录，此操作无法撤销。</p><button class="primary" data-confirm-clear>确认删除</button>');
if('confirmClear'in d){localStorage.removeItem('wellness-reports');records();}
if(d.nav==='首页')panel.close();
if(d.nav==='咨询')listing('咨询 · 心理师');
if(d.nav==='消息')openPanel('消息','<p class="empty">暂无消息</p><p class="notice">消息服务尚未连接。</p>');
if(d.nav==='我的')openPanel('小晴 · 个人中心','<button class="choice" data-records>测评记录</button><p class="notice">测评记录保存在当前浏览器。</p>');
if('records'in d)records();
});