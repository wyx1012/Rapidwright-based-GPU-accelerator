package com.xilinx.rapidwright.rwroute;

final class F3RoundAdmission {
    private static final long RESOURCE_BOUNDARY = 192L << 20;
    private static final int DRAIN_OVERUSE = 32;
    private static final int STOP_STEPS = 16;
    static final int FAST_BUDGET = 8000;

    enum Gate {DEFER, HOLD, NEED_FULL_QUOTE, ADMIT, DRAIN, STOP}
    enum Boundary {CONTINUE, DRAIN, STOP, CONVERGED}

    private final long coldFloorNs;
    private final boolean huge;
    private int best = Integer.MAX_VALUE;
    private int stagnant;
    private boolean draining;
    private boolean stopped;
    private int round;
    private int candidates;
    private long remainingNs;
    private Gate next = Gate.DEFER;

    F3RoundAdmission(long serializedBytes, long coldFloorNs) {
        if (serializedBytes < 0 || coldFloorNs <= 0) throw new IllegalArgumentException("F3 input/quote");
        huge = serializedBytes >= RESOURCE_BOUNDARY;
        this.coldFloorNs = coldFloorNs;
        System.out.printf("F3_ROUND_POLICY bytes=%d tier=%s budget=%d coldFloorNs=%d%n",
                serializedBytes, huge ? "huge" : "standard", FAST_BUDGET, coldFloorNs);
    }

    static Gate evaluate(int candidates, long remainingNs, long costNs, boolean fullQuote) {
        if (candidates < 0 || remainingNs < 0 || costNs <= 0 || (candidates == 0 && remainingNs != 0)) {
            throw new IllegalArgumentException("F3 invalid stage evidence");
        }
        if (candidates == 0 || remainingNs <= costNs) return Gate.HOLD;
        return fullQuote ? Gate.ADMIT : Gate.NEED_FULL_QUOTE;
    }

    static long safeCrossing(int pops, boolean queueIdle, long firstNs, long elapsedNs) {
        return firstNs < 0 && pops > FAST_BUDGET && !queueIdle ? elapsedNs : firstNs;
    }

    void begin(int round) {
        if (round != this.round + 1) throw new IllegalStateException("F3 round sequence");
        this.round = round;
        System.out.printf("F3_LIVE_GATE round=%d decision=%s sourceRound=%d candidates=%d remainingNs=%d quoteNs=%d%n",
                round, next, round-1, candidates, remainingNs, coldFloorNs);
        if (next == Gate.ADMIT || next == Gate.NEED_FULL_QUOTE || next == Gate.STOP) {
            throw new IllegalStateException("F3 adaptive CPU arm left its measured envelope: " + next);
        }
        candidates = 0;
        remainingNs = 0;
    }

    void cpu(int sequence, int connection, int pops, long crossing, long searchNs) {
        if (pops < 0 || searchNs <= 0 || crossing > searchNs || (crossing >= 0 && pops <= FAST_BUDGET)) {
            throw new IllegalArgumentException("F3 CPU evidence");
        }
        if (crossing >= 0) {
            candidates++;
            remainingNs = Math.addExact(remainingNs,searchNs-crossing);
        }
    }

    Boundary boundary(int overuse, boolean idle, boolean converged) {
        if (overuse < 0 || (converged && overuse != 0)) throw new IllegalArgumentException("F3 congestion evidence");
        if (converged) return Boundary.CONVERGED;
        if (stopped) return Boundary.STOP;
        if (draining || (overuse <= DRAIN_OVERUSE && idle)) {
            draining = true;
            return Boundary.DRAIN;
        }
        if (overuse < best) {
            best = overuse;
            stagnant = 0;
        } else {
            stagnant++;
        }
        if (overuse > DRAIN_OVERUSE && stagnant >= STOP_STEPS) {
            stopped = true;
            return Boundary.STOP;
        }
        return Boundary.CONTINUE;
    }

    void end(int overuse, int unroutable, int attempts) {
        Boundary state = boundary(overuse,true,overuse==0 && unroutable==0);
        switch (state) {
            case CONTINUE: next = evaluate(candidates,remainingNs,coldFloorNs,false); break;
            case DRAIN: next = Gate.DRAIN; break;
            case STOP: next = Gate.STOP; break;
            default: next = Gate.HOLD;
        }
        System.out.printf("F3_LIVE_BOUNDARY round=%d over=%d unroutable=%d attempts=%d candidates=%d "
                        + "remainingNs=%d state=%s next=%s%n",
                round,overuse,unroutable,attempts,candidates,remainingNs,state,next);
    }

    private static void require(boolean ok) {
        if (!ok) throw new AssertionError("F3 round-policy qualification");
    }

    public static void main(String[] args) {
        require(!new F3RoundAdmission(RESOURCE_BOUNDARY-1,10).huge);
        require(new F3RoundAdmission(RESOURCE_BOUNDARY,10).huge);
        require(new F3RoundAdmission(RESOURCE_BOUNDARY+1,10).huge);
        require(evaluate(0,0,10,true)==Gate.HOLD);
        require(evaluate(1,9,10,true)==Gate.HOLD);
        require(evaluate(1,10,10,true)==Gate.HOLD);
        require(evaluate(1,11,10,false)==Gate.NEED_FULL_QUOTE);
        require(evaluate(1,11,10,true)==Gate.ADMIT);
        require(safeCrossing(8000,false,-1,5)==-1);
        require(safeCrossing(8001,true,-1,5)==-1);
        require(safeCrossing(8001,false,-1,5)==5);
        require(safeCrossing(9000,false,5,8)==5);
        F3RoundAdmission c = new F3RoundAdmission(100,10);
        require(c.boundary(33,true,false)==Boundary.CONTINUE);
        for (int i=0;i<15;i++) require(c.boundary(33,true,false)==Boundary.CONTINUE);
        require(c.boundary(33,true,false)==Boundary.STOP);
        require(c.boundary(70,true,false)==Boundary.STOP);
        c = new F3RoundAdmission(100,10);
        require(c.boundary(32,false,false)==Boundary.CONTINUE);
        require(c.boundary(32,true,false)==Boundary.DRAIN);
        require(c.boundary(70,false,false)==Boundary.DRAIN);
        require(c.boundary(0,true,true)==Boundary.CONVERGED);
        c = new F3RoundAdmission(100,10);
        require(c.boundary(100,true,false)==Boundary.CONTINUE);
        for (int i=0;i<15;i++) require(c.boundary(100,true,false)==Boundary.CONTINUE);
        require(c.boundary(99,true,false)==Boundary.CONTINUE);
        require(c.boundary(100,true,false)==Boundary.CONTINUE);
        System.out.println("F3_ROUND_POLICY_TEST PASS");
    }
}
