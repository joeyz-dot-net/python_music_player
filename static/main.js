let lastPlayTime = 0;
function play(rel) {
  const now = Date.now();
  if (now - lastPlayTime < 3000) return; // 3秒内只允许一次播放
  lastPlayTime = now;

  // 移除所有高亮
  document.querySelectorAll(".track").forEach(div => {
    div.classList.remove("playing");
  });

  // 添加高亮到当前播放项
  const current = document.querySelector(`.track[onclick*="'${rel}'"]`);
  if (current) current.classList.add("playing");

  fetch("/play", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: "path=" + encodeURIComponent(rel)
  })
  .then(res => res.text())
  .then(data => {
    // 不再 reload，直接高亮
    // location.reload();
  });
}

let lastVolumeSetTime = 0;
function setVolume(val) {
  const now = Date.now();
  if (now - lastVolumeSetTime < 1000) return; // 1秒内只允许一次
  lastVolumeSetTime = now;
  fetch("/volume", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: "level=" + val
  });
}

document.addEventListener("DOMContentLoaded", function() {
  // 目录卡片点击展开/收起
  document.querySelectorAll('.dir-item').forEach(function(item) {
    item.addEventListener('click', function(e) {
      // 避免点击歌曲时触发目录展开
      if (e.target.closest('.track')) return;
      // 只展开当前，收起其他
      document.querySelectorAll('.dir-item.active').forEach(function(active) {
        if (active !== item) active.classList.remove('active');
      });
      item.classList.toggle('active');
    });
  });
});
