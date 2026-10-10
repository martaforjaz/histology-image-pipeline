function results = run_registration_batch(tissue_root, selection_csv, execute)
% Run independent CODA batches for each physical slide, using S210 as anchor.
% selection_csv: verified batch.csv; '' scans all tissue subfolders.
% execute defaults false (preflight). Existing outputs are never overwritten.
% Examples:
% run_registration_batch(root,'C:\project\reports\registration_pilot\batch.csv',true)
% run_registration_batch(root,'',false) % inspect all subfolders
if nargin<2; selection_csv=''; end
if nargin<3; execute=false; end
assert(isfolder(tissue_root),'Tissue root does not exist');
if isempty(selection_csv)
    dirs=dir(tissue_root);dirs=dirs([dirs.isdir]);
    ids=string({dirs.name})';ids=ids(~ismember(ids,[".",".."]));
else
    selected=readtable(selection_csv,'TextType','string','Delimiter',',','VariableNamingRule','preserve');
    ids=selected.slide_id;
end
assert(numel(unique(ids))==numel(ids),'Duplicate slide IDs');
suffix=["S210","S360","Leica","Olympus","P1000","Pramana","Roche","Zeiss"];
stamp=char(datetime('now','Format','yyyyMMdd_HHmmss'));
results=table('Size',[0 3],'VariableTypes',{'string','string','string'}, ...
    'VariableNames',{'slide_id','status','detail'});
if execute
    assert(license('test','image_toolbox'),'Image Processing Toolbox required');
    audit=fullfile(tissue_root,'batch_registration_logs');
    if ~isfolder(audit);mkdir(audit);end
    logfile=fullfile(audit,['batch_',stamp,'.csv']);
end
for k=1:numel(ids)
    sid=ids(k);status="skipped";detail="";
    try
        assert(~contains(sid,["/","\",".."]),'Invalid slide directory name');
        ihc="Hum Liv PDAC "+["CD1A","CD20","CD68","CD163","CK8","Collagen IV","Ki-67","vimentin"];
        assert(~ismember(sid,ihc),'IHC excluded from this H&E registration batch');
        folder=fullfile(tissue_root,char(sid),'2x');
        expected=sid+"_"+suffix+".tif";
        ff=dir(fullfile(folder,'*.tif'));
        assert(numel(ff)==8 && isequal(sort(string({ff.name})),sort(expected)), ...
            'Need exactly one TIFF for each of the eight scanners');
        other=dir(folder);other=other(~[other.isdir]);
        assert(all(ismember(string({other.name}),expected)), ...
            'Unexpected/partial files in input folder');
        assert(~isfolder(fullfile(folder,'registered')) && ~isfolder(fullfile(folder,'TA')), ...
            'Existing registration/masks: review provenance or use a separate working copy');
        before=ff;
        for j=1:8
            p=fullfile(folder,ff(j).name);info=imfinfo(p);
            assert(numel(info)==1,'Expected single-page 2x TIFF');
            unit=lower(string(info.ResolutionUnit));
            if unit=="centimeter";scale=10000;
            elseif unit=="inch";scale=25400;
            else;error('Missing physical resolution units: %s',ff(j).name);end
            assert(abs(scale/info.XResolution-5)<.01 && abs(scale/info.YResolution-5)<.01, ...
                'Expected 5 um/pixel in both axes');
            im=imread(p);assert(ndims(im)==3 && size(im,3)==3,'Expected RGB');clear im
        end
        after=dir(fullfile(folder,'*.tif'));
        assert(isequal({before.name},{after.name}) && isequal([before.bytes],[after.bytes]) ...
            && isequal([before.datenum],[after.datenum]),'Inputs changed during validation');
        ref=find(string({ff.name})==sid+"_S210.tif");assert(isscalar(ref));
        if execute
            % Record configuration outside input folder so eligibility stays strict.
            image=string({ff.name})';scanner=strings(8,1);
            for j=1:8
                q=find(expected==image(j));scanner(j)=suffix(q);
            end
            manifest=fullfile(audit,char(sid+"_"+stamp+"_scanners.csv"));
            writetable(table(image,scanner),manifest);
            rng(20261008,'twister');
            run_registration(folder,0,ref,[], 'mixed',manifest,5);
            warps=fullfile(folder,'registered','elastic registration','save_warps');
            for j=1:8
                [~,stem]=fileparts(ff(j).name);
                assert(isfile(fullfile(warps,[stem,'.mat'])),'Missing global/reference output');
                if j~=ref
                    assert(isfile(fullfile(warps,'D',[stem,'.mat'])),'Missing elastic output');
                end
            end
            status="computed_pending_visual_qc";
        else
            status="ready";
        end
        detail="S210 anchor; 5 um/pixel; H&E; automatic CODA tissue masks";
    catch failure
        detail=string(failure.message);
        if execute;status="skipped_or_failed";end
    end
    results(end+1,:)={sid,status,detail}; %#ok<AGROW>
    fprintf('%s: %s — %s\n',sid,status,detail);
    if execute;writetable(results,logfile);end
end
end

