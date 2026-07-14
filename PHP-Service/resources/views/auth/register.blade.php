<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sign Up</title>
</head>
<body>
    <h1>Sign Up</h1>

    @if ($errors->any())
        <ul>
            @foreach ($errors->all() as $error)
                <li>{{ $error }}</li>
            @endforeach
        </ul>
    @endif

    <form method="POST" action="{{ route('register') }}">
        @csrf
        <label>
            Name
            <input type="text" name="name" value="{{ old('name') }}" required>
        </label><br>
        <label>
            Email
            <input type="email" name="email" value="{{ old('email') }}" required>
        </label><br>
        <label>
            Password
            <input type="password" name="password" required>
        </label><br>
        <label>
            Confirm Password
            <input type="password" name="password_confirmation" required>
        </label><br>
        <button type="submit">Create account</button>
    </form>

    <p><a href="{{ route('login') }}">Already have an account?</a></p>
</body>
</html>
