package com.xilinx.rapidwright.rwroute;

import org.json.JSONObject;

final class F4DiversionSmoke {
    private static final JSONObject QUOTE = new JSONObject(
            "{\"cold_upper_ns\":100,\"warm_ns_by_width\":[0,10,16]}");

    private static void require(boolean condition) {
        if (!condition) throw new AssertionError("PPT 11 admission");
    }

    public static void main(String[] args) {
        int[] cuts = {8_000, 262_144, 1_048_576};
        for (int tier = 0; tier < cuts.length; ++tier) {
            int cut = F4RoundDiversion.budget(tier);
            require(cut == cuts[tier] && !F4RoundDiversion.crossed(cut, cut, true)
                    && !F4RoundDiversion.crossed(cut + 1, cut, false)
                    && F4RoundDiversion.crossed(cut + 1, cut, true));
        }
        var admission = new F4RoundDiversion(2, 192L << 20, QUOTE);
        require(!admission.admits());
        require(admission.endRound(1, 100, 100, 0, 0, 0).getString("next").equals("HOLD"));
        admission.cpu(null, 0, 8_100, 500, 100, 8_001, false);
        admission.cpu(null, 1, 262_300, 500, 100, 262_145, false);
        var result = new JSONObject(admission.endRound(2, 90, 86, 0, 2, 270_400).toString());
        require(admission.admits() && result.getJSONArray("heavy_by_service").getLong(1) == 1
                && result.getJSONArray("estimated_batches_by_width").getLong(2) == 1);
        admission.gpu(2, 70);
        admission.cpu(null, 1, 262_145, 100, 100, 262_145, true);
        result = new JSONObject(admission.endRound(3, 80, 80, 0, 1, 262_145).toString());
        require(result.getJSONArray("budget_stops_by_service").getLong(1) == 1
                && result.getLong("setup_ns") == 0 && admission.admits());
        require(admission.endRound(4, 32, 30, 3, 0, 0).getString("next").equals("DRAIN"));
        require(admission.endRound(5, 40, 40, 0, 0, 0).getString("next").equals("DRAIN"));
        var wide = new F4RoundDiversion(2, 10, QUOTE);
        for (int round = 1; round <= 16; ++round)
            require(!wide.endRound(round, 55, 55, 0, 0, 0).getString("next").equals("STOP"));
        require(wide.endRound(17, 55, 55, 0, 0, 0).getString("next").equals("STOP"));
        System.out.println("PPT11 diversion smoke passed");
    }
}
