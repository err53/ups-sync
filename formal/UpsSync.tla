---------------------------- MODULE UpsSync ----------------------------
(***************************************************************************)
(* Formal model of the ups-sync outage controller.                         *)
(*                                                                         *)
(* Physical chain:  grid -> Delta Pro Ultra (DPU) -> manual transfer       *)
(* switch -> APC Smart-UPS X 1500 -> outlet group 1 (Critical: network,    *)
(* HA) and outlet group 2 (Servers).                                       *)
(*                                                                         *)
(* One tick = 2 minutes of wall-clock time.  APC battery energy `e` is     *)
(* counted in minutes of full-load runtime: DRAIN_HI = 2 per tick with     *)
(* servers powered, DRAIN_LO = 1 per tick with only group 1 drawing.       *)
(*                                                                         *)
(* The environment (grid, switch, DPU charge, HA crashes, server timing,   *)
(* network drop before power-off, NMC auto-return) is fully adversarial.   *)
(* The controller sees only what Home Assistant can actually observe:      *)
(* APC status and runtime via NUT, group 2 state via SNMP, whether the     *)
(* SSH poweroff command succeeded, and ping reachability of the servers.   *)
(* It never sees the DPU.                                                  *)
(***************************************************************************)
EXTENDS Naturals, TLC

CONSTANTS
  EMAX,        \* APC energy when full
  DRAIN_HI,    \* energy lost per tick on battery, servers powered
  DRAIN_LO,    \* energy lost per tick on battery, group 2 off
  CHARGE,      \* energy gained per tick on input
  DMAX,        \* max ticks the DPU can carry the load on its own battery
  SMAX,        \* max ticks a server needs from poweroff command to off
  NET_TAIL,    \* max ticks a shutting-down server is unpingable before off
  BMAX,        \* max ticks a server needs to boot
  T_OB,        \* controller: ticks on battery before shutdown is started
  T_GRACE,     \* controller: ticks servers must be unpingable (after ack) before cut
  T_DEAD,      \* controller: ticks servers unpingable without ack -> assume off
  T_WAIT,      \* controller: SHUTDOWN taking longer than this raises an alarm (notification only)
  T_HOLD,      \* controller: ticks input must be stable before restore
  E_RESTORE,   \* controller: min energy before restore
  E_SHED,      \* NMC load shed: group 2 off when on battery and e < E_SHED
  E_LB,        \* UPS low-battery threshold (NUT "LB")
  E_RETURN,    \* NMC min return runtime for auto power-on of group 2
  T_SRV_OB,    \* server backstop: shut down after this many ticks on battery (0 = off)
  HA_CAN_FAIL, \* BOOLEAN: HA may crash/reboot at any time
  DIRECT_MON,  \* BOOLEAN: servers watch the NMC directly (TRUE) or via HA's upsd (FALSE)
  GS,          \* grid sensor: "off" | "true" | "any"
  T_GS         \* controller: ticks the grid sensor must read down before shutdown is started

VARIABLES
  grid,     \* "UP" | "DOWN"
  sw,       \* "GRID" | "DPU"   transfer switch position
  dpu,      \* 0..DMAX          ticks of DPU battery output remaining
  e,        \* 0..EMAX          APC battery energy
  g2,       \* BOOLEAN          outlet group 2 on
  g2by,     \* "none" | "ha" | "nmc"   who turned group 2 off
  srv,      \* "RUN" | "SHUT" | "OFF" | "BOOT"
  srvT,     \* ticks remaining in SHUT or BOOT
  net,      \* BOOLEAN          server answers ping
  srvObT,   \* server-side on-battery counter (backstop)
  p2prev,   \* BOOLEAN          group 2 had power at end of last tick (BIOS edge)
  ha,       \* "UP" | "DOWN"
  ctl,      \* "IDLE" | "SHUTDOWN" | "CUT"
  acked,    \* BOOLEAN  HA's poweroff command was accepted by the server
  obT,      \* controller on-battery counter
  darkT,    \* controller ticks servers unpingable (in SHUTDOWN)
  waitT,    \* controller ticks in SHUTDOWN
  holdT,    \* controller ticks input stable (in CUT)
  deadT,    \* ticks servers dark with power on (persisted across HA restarts)
  gs,       \* "UP" | "DOWN"  what the grid sensor reads
  gsT,      \* controller ticks the grid sensor has read down
  crashed,  \* BOOLEAN  a server lost power while RUN/SHUT/BOOT  (safety)
  timedOut, \* BOOLEAN  controller hit T_WAIT                    (safety)
  spurious  \* BOOLEAN  shutdown started without outage evidence  (safety)

vars == << grid, sw, dpu, e, g2, g2by, srv, srvT, net, srvObT, p2prev, ha,
           ctl, acked, obT, darkT, waitT, holdT, deadT, gs, gsT, crashed, timedOut, spurious >>

Min(a, b) == IF a < b THEN a ELSE b
Max(a, b) == IF a > b THEN a ELSE b
Sat(x, cap) == Min(x, cap)

-----------------------------------------------------------------------------
(* Derived physical state (pre-state) *)

ApcIn  == \/ sw = "GRID" /\ grid = "UP"
          \/ sw = "DPU"  /\ (grid = "UP" \/ dpu > 0)
UpsOut == ApcIn \/ e > 0                 \* UPS output alive (group 1 powered)
Power2 == UpsOut /\ g2                   \* servers have power
Reach  == UpsOut /\ net                  \* HA can ping / ssh the server
LB     == ~ApcIn /\ e < E_LB             \* NUT reports LB
LoadHigh == Power2 /\ srv /= "OFF"       \* UPS output load above the group-1 + standby baseline
SrvSignal == UpsOut /\ (DIRECT_MON \/ ha = "UP")   \* server can see UPS status

HaUp == ha = "UP"

\* Optional grid sensor: something in Home Assistant that only works on grid
\* power. GS = "off": not configured. "true": it always tells the truth.
\* "any": it may be wrong in either direction at any time.
GsDown == GS /= "off" /\ gs = "DOWN"
GsChoices(g) == IF GS = "off" THEN {"UP"} ELSE IF GS = "true" THEN {g} ELSE {"UP", "DOWN"}

(* Controller decisions, from observables only *)
\* Reconciliation: the controller's state is anchored to the observable group 2 state.
\* Group off while IDLE/SHUTDOWN (e.g. after an HA reboot) -> CUT.  Group on while CUT -> IDLE.
Reconcile == HaUp /\ ((ctl /= "CUT" /\ ~g2) \/ (ctl = "CUT" /\ g2))
DoShut   == HaUp /\ ctl = "IDLE" /\ g2
            /\ ((~ApcIn /\ obT >= T_OB) \/ LB \/ (GsDown /\ gsT >= T_GS))
DoRecov  == HaUp /\ ctl = "IDLE" /\ Power2 /\ ~Reach /\ ~LoadHigh /\ deadT >= T_DEAD
InShut   == HaUp /\ (ctl = "SHUTDOWN" \/ DoShut) /\ g2
HaCmd    == InShut /\ srv = "RUN" /\ Reach          \* ssh poweroff succeeds this tick
DoCutOk  == HaUp /\ ctl = "SHUTDOWN" /\ g2 /\ ~Reach /\ ~LoadHigh
            /\ ((acked /\ darkT >= T_GRACE) \/ darkT >= T_DEAD)
DoCutTO  == HaUp /\ ctl = "SHUTDOWN" /\ g2 /\ waitT >= T_WAIT /\ ~DoCutOk
DoRest   == HaUp /\ ctl = "CUT" /\ ~g2 /\ ApcIn /\ ~GsDown /\ holdT >= T_HOLD /\ e >= E_RESTORE

(* Server backstop (its own NUT client) *)
SrvBackstop == srv = "RUN" /\ SrvSignal
               /\ (LB \/ (T_SRV_OB > 0 /\ ~ApcIn /\ srvObT >= T_SRV_OB))

(* NMC load shed *)
NmcShed == ~ApcIn /\ e < E_SHED /\ g2

CmdOff == HaCmd \/ SrvBackstop

NmcReturn    == ~g2 /\ g2by = "nmc" /\ ApcIn /\ e >= E_RETURN   \* NMC auto power-on after min return runtime (deterministic)
NmcMayReturn == ~g2 /\ g2by = "ha"  /\ ApcIn /\ e >= E_RETURN   \* unknown whether an SNMP-off group also auto-returns: allow both

TypeOK ==
  /\ grid \in {"UP", "DOWN"}
  /\ sw \in {"GRID", "DPU"}
  /\ dpu \in 0..DMAX
  /\ e \in 0..EMAX
  /\ g2 \in BOOLEAN
  /\ g2by \in {"none", "ha", "nmc"}
  /\ srv \in {"RUN", "SHUT", "OFF", "BOOT"}
  /\ srvT \in 0..Max(SMAX, BMAX)
  /\ net \in BOOLEAN
  /\ srvObT \in 0..T_SRV_OB
  /\ p2prev \in BOOLEAN
  /\ ha \in {"UP", "DOWN"}
  /\ ctl \in {"IDLE", "SHUTDOWN", "CUT"}
  /\ acked \in BOOLEAN
  /\ obT \in 0..T_OB
  /\ darkT \in 0..T_DEAD
  /\ waitT \in 0..T_WAIT
  /\ holdT \in 0..T_HOLD
  /\ deadT \in 0..T_DEAD
  /\ gs \in {"UP", "DOWN"} /\ gsT \in 0..T_GS
  /\ crashed \in BOOLEAN /\ timedOut \in BOOLEAN /\ spurious \in BOOLEAN

Init ==
  /\ grid = "UP"
  /\ sw \in {"GRID", "DPU"}
  /\ dpu = DMAX
  /\ e = EMAX
  /\ g2 = TRUE /\ g2by = "none"
  /\ srv = "RUN" /\ srvT = 0 /\ net = TRUE /\ srvObT = 0
  /\ p2prev = TRUE
  /\ ha = "UP"
  /\ ctl = "IDLE" /\ acked = FALSE
  /\ obT = 0 /\ darkT = 0 /\ waitT = 0 /\ holdT = 0 /\ deadT = 0
  /\ gs = "UP" /\ gsT = 0
  /\ crashed = FALSE /\ timedOut = FALSE /\ spurious = FALSE

-----------------------------------------------------------------------------
(* One tick. Decisions are taken on the pre-state, then physics advance.  *)

DpuChoices(g) ==
  IF grid = "UP" /\ g = "DOWN" THEN 0..DMAX           \* outage begins: DPU at any charge
  ELSE IF g = "UP" THEN {DMAX}                        \* grid present: DPU recharged
  ELSE IF sw = "DPU" /\ dpu > 0 THEN {dpu - 1}        \* DPU carrying the load
  ELSE {dpu}

HaChoices == IF HaUp /\ HA_CAN_FAIL THEN {"UP", "DOWN"} ELSE {"UP"}   \* crash is an env choice; power gating in Next

Next ==
  \E g \in {"UP", "DOWN"} : \E d \in DpuChoices(g) :
  \E s \in {"GRID", "DPU"},
     gsN \in GsChoices(g),
     haNext \in HaChoices,
     shutLen \in (IF CmdOff THEN 1..SMAX ELSE {0}),
     bootLen \in 1..BMAX,
     netDrop \in (IF srv = "SHUT" \/ CmdOff THEN BOOLEAN ELSE {FALSE}),
     nmcRet  \in (IF NmcMayReturn THEN BOOLEAN ELSE {FALSE}) :
    LET
      \* ---- group 2 after this tick
      haCut    == DoCutOk \/ DoRecov            \* the T_WAIT timeout is an alarm only, never a cut
      \* ---- physics
      eNext    == IF ApcIn THEN Min(EMAX, e + CHARGE)
                  ELSE Max(0, e - (IF Power2 /\ srv /= "OFF" THEN DRAIN_HI ELSE DRAIN_LO))
      apcInN   == \/ s = "GRID" /\ g = "UP"
                  \/ s = "DPU"  /\ (g = "UP" \/ d > 0)
      upsOutN  == apcInN \/ eNext > 0
      \* ---- group 2 after this tick
      g2Cut    == haCut \/ NmcShed \/ ~upsOutN       \* UPS dies: all groups off, NMC owns the return
      g2On     == DoRest \/ NmcReturn \/ nmcRet
      g2Next   == IF g2Cut THEN FALSE ELSE IF g2On THEN TRUE ELSE g2
      g2byNext == IF g2Cut THEN (IF haCut /\ upsOutN THEN "ha" ELSE "nmc")
                  ELSE IF g2On THEN "none" ELSE g2by
      power2N  == upsOutN /\ g2Next
      haN      == IF ~upsOutN THEN "DOWN" ELSE haNext
      \* ---- server
      lostPow  == ~power2N /\ srv \in {"RUN", "SHUT", "BOOT"}
      bootEdge == srv = "OFF" /\ power2N /\ ~p2prev
      srvNext  == IF lostPow THEN "OFF"
                  ELSE IF srv = "RUN"  THEN (IF CmdOff THEN "SHUT" ELSE "RUN")
                  ELSE IF srv = "SHUT" THEN (IF srvT <= 1 THEN "OFF" ELSE "SHUT")
                  ELSE IF srv = "OFF"  THEN (IF bootEdge THEN "BOOT" ELSE "OFF")
                  ELSE (IF srvT <= 1 THEN "RUN" ELSE "BOOT")
      srvTNext == IF lostPow THEN 0
                  ELSE IF srv = "RUN" /\ CmdOff THEN shutLen
                  ELSE IF bootEdge THEN bootLen
                  ELSE IF srv \in {"SHUT", "BOOT"} THEN srvT - 1
                  ELSE 0
      netNext  == IF srvNext = "RUN" THEN TRUE
                  ELSE IF srvNext = "SHUT" THEN
                         (IF srvTNext <= NET_TAIL /\ netDrop THEN FALSE ELSE net)
                  ELSE FALSE
      \* ---- controller
      ctlNext  == IF ~HaUp \/ haN = "DOWN" THEN "IDLE"
                  ELSE IF Reconcile THEN (IF g2 THEN "IDLE" ELSE "CUT")
                  ELSE IF DoShut THEN "SHUTDOWN"
                  ELSE IF DoRecov \/ DoCutOk THEN "CUT"
                  ELSE IF DoRest THEN "IDLE"
                  ELSE ctl
    IN
    /\ grid' = g
    /\ sw' = s
    /\ dpu' = d
    /\ e' = eNext
    /\ g2' = g2Next
    /\ g2by' = g2byNext
    /\ srv' = srvNext
    /\ srvT' = srvTNext
    /\ net' = netNext
    /\ srvObT' = IF ~ApcIn THEN Sat(srvObT + 1, T_SRV_OB) ELSE 0
    /\ p2prev' = power2N
    /\ ha' = haN
    /\ ctl' = ctlNext
    /\ acked' = (IF ctlNext = "SHUTDOWN" THEN (acked \/ HaCmd) ELSE FALSE)
    /\ obT'   = IF ~HaUp THEN 0 ELSE IF ~ApcIn THEN Sat(obT + 1, T_OB) ELSE 0
    /\ darkT' = IF ctlNext = "SHUTDOWN" /\ Power2 /\ ~Reach THEN Sat(darkT + 1, T_DEAD) ELSE 0
    /\ waitT' = IF ctlNext = "SHUTDOWN" THEN Sat(waitT + 1, T_WAIT) ELSE 0
    /\ holdT' = IF ctlNext = "CUT" /\ ApcIn /\ ~GsDown THEN Sat(holdT + 1, T_HOLD) ELSE 0
    /\ gs' = gsN
    /\ gsT' = IF ~HaUp THEN 0 ELSE IF GsDown THEN Sat(gsT + 1, T_GS) ELSE 0
    \* deadT is a persisted "last time a server answered" timestamp: it survives HA restarts
    /\ deadT' = IF g2Next /\ Power2 /\ ~Reach THEN Sat(deadT + 1, T_DEAD) ELSE 0
    /\ crashed'  = (crashed \/ lostPow)
    /\ timedOut' = (timedOut \/ DoCutTO)
    /\ spurious' = (spurious \/ (DoShut /\ ApcIn /\ ~LB /\ ~GsDown))

Spec == Init /\ [][Next]_vars /\ WF_vars(Next)

-----------------------------------------------------------------------------
(* Safety *)

NoHardCut  == ~crashed     \* no server loses power while RUN / SHUT / BOOT
NoTimeout  == ~timedOut    \* the T_WAIT alarm never fires
NoSpurious == ~spurious    \* shutdown only on battery, low battery, or the grid sensor reading down

Safety == NoHardCut /\ NoTimeout /\ NoSpurious

(* Liveness: once the grid stays up and HA stops crashing, servers come back *)
(* and stay back.                                                            *)
EventuallyRunning == (<>[](grid = "UP") /\ <>[](ha = "UP") /\ <>[](gs = "UP")) => <>[](srv = "RUN")

=============================================================================
