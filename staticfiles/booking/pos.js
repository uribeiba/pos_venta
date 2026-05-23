(function(){
    const wrapper = document.getElementById("wrapper");
    const btn = document.getElementById("sidebarToggle");
    if (btn && wrapper){
      btn.addEventListener("click", () => {
        wrapper.classList.toggle("toggled");
      });
    }
  })();
  