(() => {
	let ctx = {tree:{}, musicDir:''};
	try { ctx = JSON.parse(document.getElementById('boot-data').textContent); } catch(e) { console.warn('Boot data parse error', e); }
	const ROOT = document.getElementById('tree');
	function el(tag, cls, text){ const e=document.createElement(tag); if(cls)e.className=cls; if(text) e.textContent=text; return e; }

	function buildNode(node){
		const li = el('li','dir');
		const label = el('div','label');
		const arrow = el('span','arrow','▶');
		const nameSpan = el('span','name', node.rel? node.name : '根目录');
		label.appendChild(arrow); label.appendChild(nameSpan);
		label.onclick = () => li.classList.toggle('collapsed');
		li.appendChild(label);
		const ul = el('ul');
		(node.dirs||[]).forEach(d=>ul.appendChild(buildNode(d)));
		(node.files||[]).forEach(f=>{
			const fi = el('li','file',f.name);
			fi.onclick = () => play(f.rel, fi);
			ul.appendChild(fi);
		});
		li.appendChild(ul);
		if(node.rel) li.classList.add('collapsed');
		return li;
	}

	function render(){
		ROOT.innerHTML='';
		const topUL = el('ul');
		// 展开根的子节点
		(ctx.tree.dirs||[]).forEach(d=>topUL.appendChild(buildNode(d)));
		(ctx.tree.files||[]).forEach(f=>{
			const fi = el('li','file',f.name); fi.onclick=()=>play(f.rel,fi); topUL.appendChild(fi);
		});
		ROOT.appendChild(topUL);
	}

	function play(rel, dom){
		fetch('/play', {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body:'path='+encodeURIComponent(rel)})
			.then(r=>r.json())
			.then(j=>{
				if(j.status!=='OK') { alert('播放失败: '+ j.error); return; }
				document.querySelectorAll('.file.playing').forEach(e=>e.classList.remove('playing'));
				dom.classList.add('playing');
				const bar = document.getElementById('nowPlaying');
				bar.textContent = '▶ '+ rel;
			}).catch(e=>alert('请求错误: '+ e));
	}

	function pollStatus(){
		fetch('/status').then(r=>r.json()).then(j=>{
			if(j.status!=='OK') return;
			const bar = document.getElementById('nowPlaying');
			if(!j.playing || !j.playing.rel){ bar.textContent='未播放'; return; }
			const rel = j.playing.rel;
			let label = '▶ '+ rel;
			if(j.mpv && j.mpv.time!=null && j.mpv.duration){
				const t = j.mpv.time||0, d = j.mpv.duration||0;
				const fmt = s=>{ if(isNaN(s)) return '--:--'; const m=Math.floor(s/60), ss=Math.floor(s%60); return m+':'+(ss<10?'0':'')+ss; };
				label += ' ['+ fmt(t) +' / '+ fmt(d) + (j.mpv.paused?' | 暂停':'') +']';
				// 进度条
				const wrap = document.getElementById('progWrap');
				const prog = document.getElementById('progBar');
				if(d>0){ prog.style.width = Math.min(100, t/d*100).toFixed(2)+'%'; }
				else { prog.style.width='0%'; }
			}
			bar.textContent = label;
			document.querySelectorAll('.file.playing').forEach(e=>e.classList.remove('playing'));
			// 高亮当前
			const nodes = document.querySelectorAll('#tree .file');
			nodes.forEach(n=>{ if(n.textContent === rel.split('/').pop()) { // 名称匹配最后一段
				// 进一步校验路径: 暂无全路径引用，简单设置
				n.classList.add('playing');
			}});
		}).catch(()=>{}).finally(()=> setTimeout(pollStatus, 2000));
	}

	setTimeout(pollStatus, 1500);

	document.getElementById('expandAll').onclick=()=>document.querySelectorAll('#tree .dir').forEach(d=>d.classList.remove('collapsed'));
	document.getElementById('collapseAll').onclick=()=>document.querySelectorAll('#tree .dir').forEach(d=>d.classList.add('collapsed'));
	render();
})();
