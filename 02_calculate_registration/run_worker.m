function run_worker(worker_id, manifest_path, root)
% Run one fixed shard from a shared manifest snapshot.
assert(isscalar(worker_id) && worker_id == fix(worker_id) && worker_id >= 1 && worker_id <= 8);
assert(isfile(manifest_path), 'Worker manifest is missing');
here = fileparts(mfilename('fullpath'));
assert(isfile(fullfile(here,'coda','calculate_image_registration.m')), ...
    'Install the external CODA dependency using tools/setup_coda.py');
assert(isfolder(root), 'NAS root unavailable');
assert(license('test','image_toolbox'), 'Image Processing Toolbox license unavailable');
selected = readtable(manifest_path, 'TextType','string', 'Delimiter',',', 'VariableNamingRule','preserve');
assert(isequal(selected.Properties.VariableNames, {'slide_id'}), 'Manifest needs only slide_id column');
assert(numel(unique(selected.slide_id)) == height(selected), 'Duplicate slide IDs in manifest');
fprintf('Worker %d on %s started at %s with %d slides\n', worker_id, getenv('COMPUTERNAME'), char(datetime('now')), height(selected));
results = run_registration_batch(root, manifest_path, true);
result_path = fullfile(here, sprintf('worker_%02d_results.csv', worker_id));
writetable(results, result_path);
disp(groupsummary(results, 'status'));
fprintf('Worker %d ended at %s; results: %s\n', worker_id, char(datetime('now')), result_path);
end
