(function(){
  function normalize(v){
    return String(v || "").trim().toLowerCase();
  }

  function resolveRegisteredGuard(label){
    const target = normalize(label);
    if(!target) return null;

    const matches = Object.entries(adminUserMap || {}).filter(function(entry){
      const u = entry[1] || {};
      const values = [u.email, u.name, u.label].filter(Boolean).map(normalize);
      return values.includes(target);
    });

    if(matches.length !== 1) return null;

    const id = matches[0][0];
    const u = matches[0][1] || {};
    return {
      id: id,
      name: u.name || u.label || u.email || id,
      email: u.email || ""
    };
  }

  window.selectedScheduleGuard = function(){
    const manage = document.getElementById("admin-user-manage");
    const userId = manage ? manage.value : "";
    const u = userId ? (adminUserMap[userId] || {}) : {};

    if(userId){
      return {
        id: userId,
        name: u.name || u.label || u.email || userId,
        email: u.email || ""
      };
    }

    const guardSel = document.getElementById("admin-guard-filter");
    const guard = guardSel && guardSel.value !== "__all__" ? guardSel.value : "";
    if(!guard) return null;

    const matched = resolveRegisteredGuard(guard);
    if(matched){
      if(manage) manage.value = matched.id;
      return matched;
    }

    return {
      id: "",
      name: guard,
      email: guard.includes("@") ? guard : ""
    };
  };

  const originalSendScheduleFromAdmin = window.sendScheduleFromAdmin;

  window.sendScheduleFromAdmin = async function(){
    if(!authToken) return alert("Login first.");

    let g = window.selectedScheduleGuard();
    const st = document.getElementById("schedule-modal-status");

    if(!g || !g.id){
      try{
        if(typeof loadAdminUsers === "function") await loadAdminUsers();
      }catch(e){}
      g = window.selectedScheduleGuard();
    }

    if(!g || !g.id){
      if(st) st.textContent = "This guard is not linked to a registered OpsGuard guard account. Choose the guard in Selected guard account, then tap Send Schedule again.";
      return;
    }

    return originalSendScheduleFromAdmin.apply(this, arguments);
  };
})();