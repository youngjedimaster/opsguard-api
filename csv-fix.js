(function(){
  const API_ORIGIN="https://opsguard-api.onrender.com";

  function writeDownloadShell(w){
    w.document.open();
    w.document.write("<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>OpsGuard CSV Download</title><style>body{font-family:Arial,sans-serif;padding:22px;line-height:1.45;color:#0B1F3B}a.download-btn{display:none;background:#0B1F3B;color:white;border-radius:8px;padding:12px 18px;font-weight:700;font-size:16px;text-decoration:none;width:max-content}p{max-width:680px}</style></head><body><h2>OpsGuard CSV Download</h2><p id='csv-download-status'>Preparing your CSV download...</p><a id='csv-download-button' class='download-btn' href='#'>Download CSV</a><p style='font-size:13px;color:#555'>The download should start automatically. If it does not, tap Download CSV once.</p></body></html>");
    w.document.close();
  }

  window.createCsvDownloadWindow=function(){
    let w=null;
    try{w=window.open("about:blank","_blank");}catch(e){w=null;}
    if(!w){
      alert("Popup blocked. Please allow popups for this page and try Export CSV again.");
      return null;
    }
    try{writeDownloadShell(w);}catch(e){
      try{w.close();}catch(_){}
      return null;
    }
    return w;
  };

  window.prepareServerCsvDownload=async function(w){
    if(!w||w.closed)return false;
    const status=w.document.getElementById("csv-download-status");
    const button=w.document.getElementById("csv-download-button");
    try{
      const params=buildAdminShiftQuery();
      const tokenData=await rawApi("/shifts/export-token?"+params.toString(),{method:"POST",silent:true});
      if(!tokenData||!tokenData.download_path)throw new Error("Could not create CSV download link.");
      const downloadUrl=API_ORIGIN+tokenData.download_path;

      if(button){
        button.href=downloadUrl;
        button.style.display="inline-block";
      }
      if(status)status.textContent="Your CSV is ready. Starting download...";

      setTimeout(function(){
        try{w.location.href=downloadUrl;}catch(e){}
      },100);
      return true;
    }catch(e){
      if(status)status.textContent="Could not prepare the CSV download. Return to OpsGuard and try again.";
      return false;
    }
  };

  window.saveCsvFile=async function(csvText,filename){
    const w=window.createCsvDownloadWindow();
    if(!w)return false;
    return await window.prepareServerCsvDownload(w);
  };

  window.exportAdminCsvDownload=async function(){
    const csvStatus=document.getElementById("csv-status");
    if(!authToken){alert("Login first.");return;}

    const downloadWindow=window.createCsvDownloadWindow();
    if(!downloadWindow){
      if(csvStatus)csvStatus.textContent="Popup blocked. Allow popups for this page and try Export CSV again.";
      return;
    }

    if(csvStatus)csvStatus.textContent="Preparing CSV download...";
    try{
      const ready=await window.prepareServerCsvDownload(downloadWindow);
      if(!ready)throw new Error("Could not prepare the CSV download.");

      const params=buildAdminShiftQuery();
      const data=await rawApi("/shifts?"+params.toString(),{method:"GET",silent:true});
      allShiftsCache=Array.isArray(data&&data.items)?data.items:[];
      renderAdminShifts(data||{items:[]});

      if(allShiftsCache.length){
        const csvText=buildCsvFromLoadedRows();
        const today=new Date().toISOString().slice(0,10);
        createCsvDownloadLinks(csvText,"OpsGuard_Shifts_Export_"+today+".csv",false);
      }

      if(csvStatus)csvStatus.textContent="CSV download started automatically. If Android does not save it, use the Download CSV button in the opened tab. Open CSV in new tab and Show / Copy CSV remain available below.";
    }catch(e){
      const message=e&&e.message?e.message:String(e);
      if(csvStatus)csvStatus.textContent="Export failed: "+message;
    }
  };
})();