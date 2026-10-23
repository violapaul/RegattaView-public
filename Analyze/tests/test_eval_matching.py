from Analyze.eval_starts import match_events


def test_exact_match_pairs_one_to_one():
    pred = [100.0, 200.0, 300.0]
    gt = [101.0, 199.0, 305.0]
    m = match_events(pred, gt, tol_s=15)
    assert len(m['matches']) == 3
    assert m['fp_pred'] == []
    assert m['fn_gt'] == []


def test_out_of_tolerance_is_fp_and_fn():
    pred = [100.0, 500.0]
    gt = [100.0, 800.0]
    m = match_events(pred, gt, tol_s=15)
    assert m['matches'] == [(0, 0)]
    assert m['fp_pred'] == [1]
    assert m['fn_gt'] == [1]


def test_closest_wins_when_multiple_candidates():
    # Two preds fight for one GT; the closer one wins.
    pred = [100.0, 110.0]
    gt = [108.0]
    m = match_events(pred, gt, tol_s=15)
    # Pred idx 1 is 2 s away, pred idx 0 is 8 s away -> pred 1 wins.
    assert m['matches'] == [(1, 0)]
    assert m['fp_pred'] == [0]
    assert m['fn_gt'] == []


def test_no_double_match():
    # Two confirmed labels near one prediction -> one TP, one FN.
    pred = [100.0]
    gt = [99.0, 102.0]
    m = match_events(pred, gt, tol_s=15)
    assert len(m['matches']) == 1
    assert len(m['fn_gt']) == 1


def test_empty_inputs():
    assert match_events([], [], tol_s=15) == {'matches': [], 'fp_pred': [], 'fn_gt': []}
    assert match_events([100.0], [], tol_s=15)['fp_pred'] == [0]
    assert match_events([], [100.0], tol_s=15)['fn_gt'] == [0]
