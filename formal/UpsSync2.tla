---------------------------- MODULE UpsSync2 ----------------------------
(***************************************************************************)
(* Two-server refinement of UpsSync.  Same physics and controller, but     *)
(* flandre and sakuya are modelled independently (own shutdown/boot        *)
(* timing, own ping, own ack), and a human may power any server off or on  *)
(* while input power is present.  The controller may cut group 2 only when *)
(* EVERY server is dark, and may power-cycle only when EVERY server is     *)
(* dark.                                                                   *)
(*                                                                         *)
(* Assumptions (documented): nobody presses power buttons or runs poweroff *)
(* by hand while the UPS is on battery, and nobody powers a server ON      *)
(* while the UPS has less than E_RESTORE minutes of runtime.               *)
(***************************************************************************)
EXTENDS Naturals, TLC

CONSTANTS
  Servers,
  EMAX, DRAIN_HI, DRAIN_LO, CHARGE, DMAX, SMAX, NET_TAIL, BMAX,
  T_OB, T_GRACE, T_DEAD, T_WOL, T_WAIT, T_HOLD, E_RESTORE, E_SHED, E_LB, E_RETURN,
  T_SRV_OB, HA_CAN_FAIL, DIRECT_MON,
  HUMAN        \* BOOLEAN: humans may power servers off/on while input is present

VARIABLES
  grid, sw, dpu, e, g2, g2by,
  srv, srvT, net,          \* functions Servers -> ...
  manual,                  \* function Servers -> BOOLEAN: last power-off was by a human
  manTick,                 \* BOOLEAN: a human acted this tick
  srvObT, p2prev, ha, ctl,
  acked,                   \* function Servers -> BOOLEAN
  obT, darkT, waitT, holdT, deadT,
  wolT,                    \* ticks some (not all) servers unreachable with power on (persisted)
  crashed, timedOut, spurious

vars == << grid, sw, dpu, e, g2, g2by, srv, srvT, net, manual, manTick, srvObT, p2prev, ha,
           ctl, acked, obT, darkT, waitT, holdT, deadT, wolT, crashed, timedOut, spurious >>

Syms == Permutations(Servers)

Min(a, b) == IF a < b THEN a ELSE b
Max(a, b) == IF a > b THEN a ELSE b
Sat(x, cap) == Min(x, cap)

ApcIn  == \/ sw = "GRID" /\ grid = "UP"
          \/ sw = "DPU"  /\ (grid = "UP" \/ dpu > 0)
UpsOut == ApcIn \/ e > 0
Power2 == UpsOut /\ g2
Reach(i) == UpsOut /\ net[i]
AnyReach == \E i \in Servers : Reach(i)
AnyPowered == Power2 /\ \E i \in Servers : srv[i] /= "OFF"
LB     == ~ApcIn /\ e < E_LB
LoadHigh == AnyPowered                    \* UPS output load above the group-1 + standby baseline
SrvSignal == UpsOut /\ (DIRECT_MON \/ ha = "UP")
HaUp == ha = "UP"
AllAcked == \A i \in Servers : acked[i]

Reconcile == HaUp /\ ((ctl /= "CUT" /\ ~g2) \/ (ctl = "CUT" /\ g2))
DoShut   == HaUp /\ ctl = "IDLE" /\ g2 /\ ((~ApcIn /\ obT >= T_OB) \/ LB)
DoRecov  == HaUp /\ ctl = "IDLE" /\ Power2 /\ ~AnyReach /\ ~LoadHigh /\ deadT >= T_DEAD
InShut   == HaUp /\ (ctl = "SHUTDOWN" \/ DoShut) /\ g2
HaCmd(i) == InShut /\ srv[i] = "RUN" /\ Reach(i)
DoCutOk  == HaUp /\ ctl = "SHUTDOWN" /\ g2 /\ ~AnyReach /\ ~LoadHigh
            /\ ((AllAcked /\ darkT >= T_GRACE) \/ darkT >= T_DEAD)
DoCutTO  == HaUp /\ ctl = "SHUTDOWN" /\ g2 /\ waitT >= T_WAIT /\ ~DoCutOk
DoRest   == HaUp /\ ctl = "CUT" /\ ~g2 /\ ApcIn /\ holdT >= T_HOLD /\ e >= E_RESTORE
\* Partial outage leftover: some server is down while another is up. Cycling the group would
\* hard-cut the running one, so wake the down one with Wake-on-LAN instead (harmless if it is
\* already booting). Same runtime margin as a restore.
Partial  == Power2 /\ AnyReach /\ \E i \in Servers : ~Reach(i)
DoWol    == HaUp /\ ctl = "IDLE" /\ Partial /\ ApcIn /\ e >= E_RESTORE /\ wolT >= T_WOL

SrvBackstop(i) == srv[i] = "RUN" /\ SrvSignal
                  /\ (LB \/ (T_SRV_OB > 0 /\ ~ApcIn /\ srvObT >= T_SRV_OB))
NmcShed == ~ApcIn /\ e < E_SHED /\ g2
NmcReturn    == ~g2 /\ g2by = "nmc" /\ ApcIn /\ e >= E_RETURN
NmcMayReturn == ~g2 /\ g2by = "ha"  /\ ApcIn /\ e >= E_RETURN

\* Manual actions only while input power is present
OffCand == IF HUMAN /\ ApcIn THEN {i \in Servers : srv[i] = "RUN"} ELSE {}
OnCand  == IF HUMAN /\ ApcIn /\ e >= E_RESTORE THEN {i \in Servers : srv[i] = "OFF"} ELSE {}  \* assumption: no manual power-on below restore runtime
CmdSet(mOff)  == {i \in Servers : srv[i] = "RUN" /\ (HaCmd(i) \/ SrvBackstop(i) \/ i \in mOff)}
ShutSet(mOff) == CmdSet(mOff) \cup {i \in Servers : srv[i] = "SHUT"}
OffSet == {i \in Servers : srv[i] = "OFF"}

TypeOK ==
  /\ grid \in {"UP", "DOWN"} /\ sw \in {"GRID", "DPU"} /\ dpu \in 0..DMAX /\ e \in 0..EMAX
  /\ g2 \in BOOLEAN /\ g2by \in {"none", "ha", "nmc"}
  /\ srv \in [Servers -> {"RUN", "SHUT", "OFF", "BOOT"}]
  /\ srvT \in [Servers -> 0..Max(SMAX, BMAX)]
  /\ net \in [Servers -> BOOLEAN]
  /\ manual \in [Servers -> BOOLEAN] /\ manTick \in BOOLEAN
  /\ acked \in [Servers -> BOOLEAN]
  /\ srvObT \in 0..T_SRV_OB /\ p2prev \in BOOLEAN /\ ha \in {"UP", "DOWN"}
  /\ ctl \in {"IDLE", "SHUTDOWN", "CUT"}
  /\ obT \in 0..T_OB /\ darkT \in 0..T_DEAD /\ waitT \in 0..T_WAIT /\ holdT \in 0..T_HOLD /\ deadT \in 0..T_DEAD
  /\ wolT \in 0..T_WOL
  /\ crashed \in BOOLEAN /\ timedOut \in BOOLEAN /\ spurious \in BOOLEAN

Init ==
  /\ grid = "UP" /\ sw \in {"GRID", "DPU"} /\ dpu = DMAX /\ e = EMAX
  /\ g2 = TRUE /\ g2by = "none"
  /\ srv = [i \in Servers |-> "RUN"] /\ srvT = [i \in Servers |-> 0] /\ net = [i \in Servers |-> TRUE]
  /\ manual = [i \in Servers |-> FALSE] /\ manTick = FALSE
  /\ acked = [i \in Servers |-> FALSE]
  /\ srvObT = 0 /\ p2prev = TRUE /\ ha = "UP" /\ ctl = "IDLE"
  /\ obT = 0 /\ darkT = 0 /\ waitT = 0 /\ holdT = 0 /\ deadT = 0 /\ wolT = 0
  /\ crashed = FALSE /\ timedOut = FALSE /\ spurious = FALSE

DpuChoices(g) ==
  IF grid = "UP" /\ g = "DOWN" THEN 0..DMAX
  ELSE IF g = "UP" THEN {DMAX}
  ELSE IF sw = "DPU" /\ dpu > 0 THEN {dpu - 1}
  ELSE {dpu}

HaChoices == IF HaUp /\ HA_CAN_FAIL THEN {"UP", "DOWN"} ELSE {"UP"}

Next ==
  \E g \in {"UP", "DOWN"} : \E d \in DpuChoices(g) :
  \E mOff \in SUBSET OffCand : \E mOn \in SUBSET OnCand :
  \E s \in {"GRID", "DPU"}, haNext \in HaChoices,
     nmcRet \in (IF NmcMayReturn THEN BOOLEAN ELSE {FALSE}),
     shutLen \in [CmdSet(mOff) -> 1..SMAX],
     bootLen \in [OffSet -> 1..BMAX],
     netDrop \in [ShutSet(mOff) -> BOOLEAN] :
    LET
      cmdOff(i) == i \in CmdSet(mOff)
      haCut    == DoCutOk \/ DoRecov            \* the T_WAIT timeout is an alarm only, never a cut
      eNext    == IF ApcIn THEN Min(EMAX, e + CHARGE)
                  ELSE Max(0, e - (IF AnyPowered THEN DRAIN_HI ELSE DRAIN_LO))
      apcInN   == \/ s = "GRID" /\ g = "UP"
                  \/ s = "DPU"  /\ (g = "UP" \/ d > 0)
      upsOutN  == apcInN \/ eNext > 0
      g2Cut    == haCut \/ NmcShed \/ ~upsOutN
      g2On     == DoRest \/ NmcReturn \/ nmcRet
      g2Next   == IF g2Cut THEN FALSE ELSE IF g2On THEN TRUE ELSE g2
      g2byNext == IF g2Cut THEN (IF haCut /\ upsOutN THEN "ha" ELSE "nmc")
                  ELSE IF g2On THEN "none" ELSE g2by
      power2N  == upsOutN /\ g2Next
      haN      == IF ~upsOutN THEN "DOWN" ELSE haNext
      lostPow(i) == ~power2N /\ srv[i] \in {"RUN", "SHUT", "BOOT"}
      bootEdge(i) == srv[i] = "OFF" /\ power2N /\ (~p2prev \/ i \in mOn \/ (DoWol /\ ~Reach(i)))
      srvNext(i) ==
        IF lostPow(i) THEN "OFF"
        ELSE IF srv[i] = "RUN"  THEN (IF cmdOff(i) THEN "SHUT" ELSE "RUN")
        ELSE IF srv[i] = "SHUT" THEN (IF srvT[i] <= 1 THEN "OFF" ELSE "SHUT")
        ELSE IF srv[i] = "OFF"  THEN (IF bootEdge(i) THEN "BOOT" ELSE "OFF")
        ELSE (IF srvT[i] <= 1 THEN "RUN" ELSE "BOOT")
      srvTNext(i) ==
        IF lostPow(i) THEN 0
        ELSE IF srv[i] = "RUN" /\ cmdOff(i) THEN shutLen[i]
        ELSE IF bootEdge(i) THEN bootLen[i]
        ELSE IF srv[i] \in {"SHUT", "BOOT"} THEN srvT[i] - 1
        ELSE 0
      netNext(i) ==
        IF srvNext(i) = "RUN" THEN TRUE
        ELSE IF srvNext(i) = "SHUT" THEN (IF srvTNext(i) <= NET_TAIL /\ netDrop[i] THEN FALSE ELSE net[i])
        ELSE FALSE
      manualNext(i) ==
        IF i \in mOff /\ ~(HaCmd(i) \/ SrvBackstop(i)) THEN TRUE     \* human-only power-off
        ELSE IF srvNext(i) \in {"RUN", "BOOT"} THEN FALSE
        ELSE manual[i]
      ctlNext  == IF ~HaUp \/ haN = "DOWN" THEN "IDLE"
                  ELSE IF Reconcile THEN (IF g2 THEN "IDLE" ELSE "CUT")
                  ELSE IF DoShut THEN "SHUTDOWN"
                  ELSE IF DoRecov \/ DoCutOk THEN "CUT"
                  ELSE IF DoRest THEN "IDLE"
                  ELSE ctl
    IN
    /\ grid' = g /\ sw' = s /\ dpu' = d /\ e' = eNext
    /\ g2' = g2Next /\ g2by' = g2byNext
    /\ srv'  = [i \in Servers |-> srvNext(i)]
    /\ srvT' = [i \in Servers |-> srvTNext(i)]
    /\ net'  = [i \in Servers |-> netNext(i)]
    /\ manual' = [i \in Servers |-> manualNext(i)]
    /\ manTick' = (mOff /= {} \/ mOn /= {})
    /\ srvObT' = IF ~ApcIn THEN Sat(srvObT + 1, T_SRV_OB) ELSE 0
    /\ p2prev' = power2N
    /\ ha' = haN
    /\ ctl' = ctlNext
    /\ acked' = [i \in Servers |-> IF ctlNext = "SHUTDOWN" THEN (acked[i] \/ HaCmd(i)) ELSE FALSE]
    /\ obT'   = IF ~HaUp THEN 0 ELSE IF ~ApcIn THEN Sat(obT + 1, T_OB) ELSE 0
    /\ darkT' = IF ctlNext = "SHUTDOWN" /\ Power2 /\ ~AnyReach THEN Sat(darkT + 1, T_DEAD) ELSE 0
    /\ waitT' = IF ctlNext = "SHUTDOWN" THEN Sat(waitT + 1, T_WAIT) ELSE 0
    /\ holdT' = IF ctlNext = "CUT" /\ ApcIn THEN Sat(holdT + 1, T_HOLD) ELSE 0
    \* deadT is a persisted "last time a server answered" timestamp: it survives HA restarts
    /\ deadT' = IF g2Next /\ Power2 /\ ~AnyReach THEN Sat(deadT + 1, T_DEAD) ELSE 0
    /\ wolT'  = IF g2Next /\ Partial /\ ~DoWol THEN Sat(wolT + 1, T_WOL) ELSE 0
    /\ crashed'  = (crashed \/ \E i \in Servers : lostPow(i))
    /\ timedOut' = (timedOut \/ DoCutTO)
    /\ spurious' = (spurious \/ (DoShut /\ ApcIn /\ ~LB))

Spec == Init /\ [][Next]_vars /\ WF_vars(Next)

NoHardCut  == ~crashed
NoTimeout  == ~timedOut
NoSpurious == ~spurious
Safety == NoHardCut /\ NoSpurious   \* NoTimeout checked separately with HUMAN = FALSE: a human
                                    \* repeatedly powering a server back on can legitimately make the alarm fire

\* Once grid and HA are stable and the human stops acting, every server that was not
\* deliberately powered off by a human is running.
EventuallyRunning ==
  (<>[](grid = "UP") /\ <>[](ha = "UP") /\ <>[](~manTick))
    => <>[](\A i \in Servers : manual[i] \/ srv[i] = "RUN")

=============================================================================
