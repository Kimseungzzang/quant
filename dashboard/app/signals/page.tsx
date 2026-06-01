import SignalChart from "./SignalChart";

export default function SignalsPage() {
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-bold">실시간 신호</h1>
      <SignalChart />
    </div>
  );
}
